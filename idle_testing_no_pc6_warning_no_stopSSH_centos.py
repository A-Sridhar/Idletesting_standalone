import re
import time
from datetime import datetime, timedelta

import serial
import serial.tools.list_ports


BAUD_RATE = 115200
SERIAL_READ_TIMEOUT = 2  # per-read timeout used while polling for command completion
MSR_LIST = ["0x198", "0x3fd", "0x660", "0xe2", "0x3f9"]
TURBOSTAT_PATH = "/root/apps/PI_PM/turbostat"
TURBOSTAT_CMD = "./turbostat -q -s Package,Node,Core,CPU,Bzy_MHz,IPC,Busy%,CPU%c1,CPU%c6,C1E%,CoreTmp,PkgTmp,Pkg%pc2,Pkg%pc6,PkgWatt,RAMWatt,UncMHz,Mod%c6 -interval 1 -n 5"
LOOP_INTERVAL_SECONDS = 60 * 60


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_turbostat_instance0(output_text):
    """
    Parse turbostat output and return (pkg_pc2, pkg_pc6, cpu_c1, cpu_c6)
    for instance/CPU 0.  Returns (None, None, None, None) if parsing fails.
    """
    lines = [ln.strip() for ln in output_text.splitlines() if ln.strip()]
    if not lines:
        return None, None, None, None

    header_idx = None
    for i, ln in enumerate(lines):
        low = ln.lower()
        if "pkg%pc2" in low and "pkg%pc6" in low:
            header_idx = i
            break

    if header_idx is None:
        return None, None, None, None

    headers = re.split(r"\s+", lines[header_idx])
    data_lines = lines[header_idx + 1 :]
    lower_headers = [h.lower() for h in headers]

    # Find column indices
    try:
        idx_pc2 = lower_headers.index("pkg%pc2")
        idx_pc6 = lower_headers.index("pkg%pc6")
    except ValueError:
        return None, None, None, None

    idx_c1 = lower_headers.index("cpu%c1") if "cpu%c1" in lower_headers else None
    idx_c6 = lower_headers.index("cpu%c6") if "cpu%c6" in lower_headers else None

    def extract(cols):
        pc2 = cols[idx_pc2]
        pc6 = cols[idx_pc6]
        c1  = cols[idx_c1] if idx_c1 is not None and idx_c1 < len(cols) else None
        c6  = cols[idx_c6] if idx_c6 is not None and idx_c6 < len(cols) else None
        return pc2, pc6, c1, c6

    valid_indices = [i for i in [idx_pc2, idx_pc6, idx_c1, idx_c6] if i is not None]
    max_idx = max(valid_indices)

    # Look for CPU 0 row first
    for row in data_lines:
        cols = re.split(r"\s+", row)
        if len(cols) <= max_idx:
            continue
        if cols[0] == "0":
            return extract(cols)

    # Fallback: first valid numeric row
    for row in data_lines:
        cols = re.split(r"\s+", row)
        if len(cols) <= max_idx:
            continue
        if re.match(r"^\d+(\.\d+)?$", cols[idx_pc2]) and re.match(r"^\d+(\.\d+)?$", cols[idx_pc6]):
            return extract(cols)

    return None, None, None, None


def list_com_ports():
    return list(serial.tools.list_ports.comports())


def choose_com_port():
    """Prompt the user to pick a COM port from the detected list, or type one manually."""
    ports = list_com_ports()
    if not ports:
        print("No COM ports detected. Make sure the device is connected.")
        return input("Enter COM port manually (e.g. COM3): ").strip().upper()

    print("\nAvailable COM ports:")
    for i, p in enumerate(ports, start=1):
        print(f"  [{i}] {p.device} - {p.description}")

    while True:
        choice = input(f"Select COM port [1-{len(ports)}] (or type a port name): ").strip()
        if not choice:
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(ports):
            return ports[int(choice) - 1].device
        if choice.upper().startswith("COM"):
            return choice.upper()
        print("Invalid selection. Try again.")


def connect_serial(port, baudrate=BAUD_RATE):
    ser = serial.Serial(
        port=port,
        baudrate=baudrate,
        timeout=SERIAL_READ_TIMEOUT,
        write_timeout=10,
    )
    time.sleep(0.5)  # allow the port to settle before use
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    return ser


def run_serial_command(ser, command, timeout=120):
    """
    Send a command over the serial console and capture its output.
    Returns (rc, out, err) to mirror the previous SSH-based interface.
    """
    marker = f"__END_{time.time_ns()}__"
    full_cmd = f"{command}; echo {marker}:$?"

    ser.reset_input_buffer()
    ser.write((full_cmd + "\r\n").encode("utf-8"))
    ser.flush()

    buffer = ""
    deadline = time.time() + timeout
    rc = None
    while time.time() < deadline:
        chunk = ser.read(ser.in_waiting or 1).decode(errors="replace")
        if chunk:
            buffer += chunk
            if marker in buffer:
                break
        else:
            time.sleep(0.05)

    out_lines = []
    for line in buffer.splitlines():
        stripped = line.strip()
        if not stripped or stripped == full_cmd.strip():
            continue  # skip blank lines and the echoed command
        m = re.match(rf"^{re.escape(marker)}:(-?\d+)", stripped)
        if m:
            rc = int(m.group(1))
            continue
        out_lines.append(line)

    out = "\n".join(out_lines).strip()
    err = "" if rc is not None else "Timed out waiting for command completion marker."
    if rc is None:
        rc = -1
    return rc, out, err


def main():
    port = choose_com_port()
    if not port:
        print("COM port is required.")
        return

    start_time = datetime.now()
    loop_count = 0
    safe_port_name = port.replace("/", "_").replace("\\", "_")
    log_name = f"serial_turbostat_log_{safe_port_name}_{start_time.strftime('%Y%m%d_%H%M%S')}.log"

    print(f"Logging to: {log_name}")
    print("Press Ctrl+C to stop.\n")

    with open(log_name, "a", encoding="utf-8") as log:
        log.write(f"[{now_str()}] Script started for COM port={port}\n")
        log.write(f"[{now_str()}] Interval={LOOP_INTERVAL_SECONDS} seconds\n\n")

        while True:
            loop_start = datetime.now()
            loop_count += 1
            elapsed = loop_start - start_time
            pkg_pc2 = None
            pkg_pc6 = None
            cpu_c1  = None
            cpu_c6  = None
            msr_data = {}

            try:
                ser = connect_serial(port)

                log.write(f"========== LOOP {loop_count} @ {now_str()} ==========\n")
                log.write(f"Elapsed: {str(elapsed).split('.')[0]}\n")

                # Read requested MSRs
                log.write("MSR Reads:\n")
                msr_data = {}
                for msr in MSR_LIST:
                    cmd = f"rdmsr -x {msr}"
                    rc, out, err = run_serial_command(ser, cmd, timeout=30)
                    val = out.strip() if out.strip() else "(no output)"
                    msr_data[msr] = val
                    if rc != 0:
                        log.write(f"  {msr} -> ERROR rc={rc}, err={err.strip()}\n")
                    else:
                        log.write(f"  {msr} -> {val}\n")

                # Run turbostat
                log.write("\nTurbostat output:\n")
                full_turbostat_cmd = f"cd {TURBOSTAT_PATH}; {TURBOSTAT_CMD}"
                rc, out, err = run_serial_command(ser, full_turbostat_cmd, timeout=180)
                if rc != 0:
                    log.write(f"[ERROR] turbostat rc={rc}\n")
                    if err.strip():
                        log.write(err + "\n")
                    warn_msg = f"[WARNING] Turbostat command failed with rc={rc}"
                    print(f"\n*** {warn_msg} ***")
                    log.write(f"\n{warn_msg}\n")
                else:
                    log.write(out + "\n")
                    pkg_pc2, pkg_pc6, cpu_c1, cpu_c6 = parse_turbostat_instance0(out)
                    
                    # Check for warning conditions - 0, N/A, or NA values
                    warnings = []
                    
                    # Check pkg_pc2
                    if pkg_pc2 is None or pkg_pc2 == "N/A" or pkg_pc2 == "NA" or pkg_pc2 == "0":
                        warnings.append("Pkg%pc2 is 0/N/A/NA")
                    else:
                        try:
                            if float(pkg_pc2) == 0:
                                warnings.append("Pkg%pc2 is 0")
                        except ValueError:
                            warnings.append(f"Pkg%pc2 has invalid value: {pkg_pc2}")
                    
                    # Check pkg_pc6
                    if pkg_pc6 is None or pkg_pc6 == "N/A" or pkg_pc6 == "NA" or pkg_pc6 == "0":
                        warnings.append("Pkg%pc6 is 0/N/A/NA")
                    else:
                        try:
                            if float(pkg_pc6) == 0:
                                warnings.append("Pkg%pc6 is 0")
                        except ValueError:
                            warnings.append(f"Pkg%pc6 has invalid value: {pkg_pc6}")
                    
                    # Check cpu_c1
                    if cpu_c1 is not None and cpu_c1 != "N/A" and cpu_c1 != "NA":
                        try:
                            if float(cpu_c1) == 0:
                                warnings.append("CPU%c1 is 0")
                        except ValueError:
                            pass
                    
                    # Check cpu_c6
                    if cpu_c6 is not None and cpu_c6 != "N/A" and cpu_c6 != "NA":
                        try:
                            if float(cpu_c6) == 0:
                                warnings.append("CPU%c6 is 0")
                        except ValueError:
                            pass
                    
                    # Check MSR values for 0 or N/A
                    for msr_key in MSR_LIST:
                        msr_val = msr_data.get(msr_key, "").strip()
                        if msr_val.upper() in ["N/A", "NA"] or msr_val == "0" or msr_val == "0x0":
                            warnings.append(f"MSR {msr_key} is 0/N/A/NA: {msr_val}")
                        elif msr_val and msr_val != "(no output)":
                            try:
                                if int(msr_val, 0) == 0:
                                    warnings.append(f"MSR {msr_key} is 0")
                            except ValueError:
                                pass
                    
                    if warnings:
                        warn_msg = f"[WARNING] {'; '.join(warnings)}"
                        print(f"\n*** {warn_msg} ***")
                        log.write(f"\n{warn_msg}\n")

                log.write("===============================================\n\n")
                log.flush()

                ser.close()

            except (serial.SerialException, OSError, TimeoutError) as e:
                warn_msg = f"[WARNING] Serial connection failed: {e}"
                print(f"\n*** {warn_msg} ***")
                log.write(f"[{now_str()}] LOOP {loop_count} SERIAL ERROR: {e}\n\n")
                log.flush()
            except Exception as e:
                log.write(f"[{now_str()}] LOOP {loop_count} UNEXPECTED ERROR: {e}\n\n")
                log.flush()

            elapsed_now = datetime.now() - start_time
            elapsed_text = str(elapsed_now).split(".")[0]
            pc2_text = pkg_pc2 if pkg_pc2 is not None else "N/A"
            pc6_text = pkg_pc6 if pkg_pc6 is not None else "N/A"
            c1_text  = cpu_c1  if cpu_c1  is not None else "N/A"
            c6_text  = cpu_c6  if cpu_c6  is not None else "N/A"

            # Format MSR data for display
            msr_display = " | ".join([f"{msr}={msr_data.get(msr, 'N/A')}" for msr in MSR_LIST])

            print(
                f"Loop={loop_count} | Elapsed={elapsed_text} | "
                f"CPU%c1={c1_text} | CPU%c6={c6_text} | "
                f"Pkg%pc2={pc2_text} | Pkg%pc6={pc6_text} | "  # instance 0
                f"{msr_display}"
            )

            # Sleep until next 60-minute boundary relative to loop start
            next_run = loop_start + timedelta(seconds=LOOP_INTERVAL_SECONDS)
            sleep_seconds = max(0, (next_run - datetime.now()).total_seconds())
            if sleep_seconds > 0:
                while sleep_seconds > 0:
                    remaining = int(sleep_seconds)
                    mins, secs = divmod(remaining, 60)
                    hours, mins = divmod(mins, 60)
                    print(
                        f"Next loop in {hours:02d}:{mins:02d}:{secs:02d}",
                        end="\r",
                        flush=True,
                    )
                    time.sleep(1)
                    sleep_seconds = max(0, (next_run - datetime.now()).total_seconds())
                print(" " * 50, end="\r", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting.")
