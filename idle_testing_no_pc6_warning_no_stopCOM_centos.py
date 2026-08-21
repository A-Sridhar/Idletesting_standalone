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
    Parse turbostat output covering all sampled intervals (turbostat repeats the
    header for each -n sample). Picks the sample interval whose CPU-0 row has the
    highest Pkg%pc6 (deepest package idle reached), and returns Pkg%pc2, Pkg%pc6,
    CPU%c1, and CPU%c6 all from that same sample/row (not mixed across samples).
    Returns (None, None, None, None) if parsing fails.
    """
    lines = [ln.strip() for ln in output_text.splitlines() if ln.strip()]
    if not lines:
        return None, None, None, None

    idx_pc2 = idx_pc6 = idx_c1 = idx_c6 = None
    max_idx = -1
    best_pc6_val = None
    best = None  # (pc2, pc6, c1, c6) from the sample with the highest Pkg%pc6

    for line in lines:
        low = line.lower()
        if "pkg%pc2" in low and "pkg%pc6" in low:
            headers = re.split(r"\s+", line)
            lower_headers = [h.lower() for h in headers]
            try:
                idx_pc2 = lower_headers.index("pkg%pc2")
                idx_pc6 = lower_headers.index("pkg%pc6")
            except ValueError:
                idx_pc2 = idx_pc6 = None
            idx_c1 = lower_headers.index("cpu%c1") if "cpu%c1" in lower_headers else None
            idx_c6 = lower_headers.index("cpu%c6") if "cpu%c6" in lower_headers else None
            valid_indices = [i for i in [idx_pc2, idx_pc6, idx_c1, idx_c6] if i is not None]
            max_idx = max(valid_indices) if valid_indices else -1
            continue

        if idx_pc2 is None or idx_pc6 is None:
            continue  # haven't seen a header yet

        cols = re.split(r"\s+", line)
        if len(cols) <= max_idx or cols[0] != "0":
            continue  # only consider the CPU 0 row for this sample

        pc2_raw = cols[idx_pc2]
        pc6_raw = cols[idx_pc6]
        try:
            pc6_num = float(pc6_raw)
            float(pc2_raw)  # validate Pkg%pc2 is numeric too
        except ValueError:
            continue

        c1_raw = cols[idx_c1] if idx_c1 is not None and idx_c1 < len(cols) else None
        c6_raw = cols[idx_c6] if idx_c6 is not None and idx_c6 < len(cols) else None

        if best_pc6_val is None or pc6_num > best_pc6_val:
            best_pc6_val = pc6_num
            best = (pc2_raw, pc6_raw, c1_raw, c6_raw)

    if best is None:
        return None, None, None, None

    return best


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


def connect_ssh(port, baudrate=BAUD_RATE):
    ssh = serial.Serial(
        port=port,
        baudrate=baudrate,
        timeout=SERIAL_READ_TIMEOUT,
        write_timeout=10,
    )
    time.sleep(0.5)  # allow the port to settle before use
    ssh.reset_input_buffer()
    ssh.reset_output_buffer()
    _drain_stale_input(ssh)
    return ssh


_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-9;?]*[a-zA-Z]|\][^\x07]*\x07|[@-Z\\-_])")


def _clean_terminal_bytes(text):
    """Strip ANSI/VT100 escape codes and apply backspaces from a real serial TTY (readline) session."""
    text = _ANSI_ESCAPE_RE.sub("", text).replace("\r", "")
    out = []
    for ch in text:
        if ch == "\x08":
            if out:
                out.pop()
        else:
            out.append(ch)
    return "".join(out)


_END_MARKER_RE = re.compile(r"__END_(\d+)__:(-?\d+)")


def _drain_stale_input(ssh, quiet_seconds=0.3, max_wait=2.0):
    """Read and discard bytes still trickling in from a previous command before starting a new one."""
    ssh.reset_input_buffer()
    deadline = time.time() + max_wait
    last_byte_time = time.time()
    while time.time() < deadline:
        chunk = ssh.read(ssh.in_waiting or 1)
        if chunk:
            last_byte_time = time.time()
        elif time.time() - last_byte_time > quiet_seconds:
            break
        else:
            time.sleep(0.02)


def run_ssh_command(ssh, command, timeout=120, max_attempts=3):
    """
    Send a command over the serial console and capture its output.
    Returns (rc, out, err) to mirror the previous SSH-based interface.
    Retries with a fresh marker if the completion line can't be matched exactly,
    which handles a lossy/reordering transport (e.g. IPMI SOL over a virtual COM port)
    that can otherwise let one command's output bleed into the next.
    """
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        marker_id = str(time.time_ns())
        full_cmd = f"{command}; echo __END_{marker_id}__:$?"

        _drain_stale_input(ssh)
        ssh.write((full_cmd + "\r\n").encode("utf-8"))
        ssh.flush()

        buffer = ""
        deadline = time.time() + timeout
        rc = None
        while time.time() < deadline:
            chunk = ssh.read(ssh.in_waiting or 1).decode(errors="replace")
            if chunk:
                buffer = _clean_terminal_bytes(buffer + chunk)
                match = _END_MARKER_RE.search(buffer)
                if match and match.group(1) == marker_id:
                    rc = int(match.group(2))
                    break
            else:
                time.sleep(0.05)

        if rc is not None:
            out_lines = []
            for line in buffer.splitlines():
                stripped = line.strip()
                if _END_MARKER_RE.search(stripped):
                    break  # stop at the completion marker; ignore the next prompt echoed after it
                if not stripped or stripped == full_cmd.strip():
                    continue  # skip blank lines and the echoed command
                if re.match(r"^\S+@\S+[:#$]\s*$|^\[.*\]\s*[#$]\s*$", stripped):
                    continue  # skip a bare shell prompt line
                out_lines.append(line)
            return rc, "\n".join(out_lines).strip(), ""

        last_err = f"Timed out waiting for completion marker (attempt {attempt}/{max_attempts})."
        time.sleep(0.3)  # let the line settle before retrying

    return -1, "", last_err


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
                ssh = connect_ssh(port)

                log.write(f"========== LOOP {loop_count} @ {now_str()} ==========\n")
                log.write(f"Elapsed: {str(elapsed).split('.')[0]}\n")

                # Read requested MSRs
                log.write("MSR Reads:\n")
                msr_data = {}
                for msr in MSR_LIST:
                    cmd = f"rdmsr -x {msr}"
                    rc, out, err = run_ssh_command(ssh, cmd, timeout=30)
                    val = out.strip() if out.strip() else "(no output)"
                    msr_data[msr] = val
                    if rc != 0:
                        log.write(f"  {msr} -> ERROR rc={rc}, err={err.strip()}\n")
                    else:
                        log.write(f"  {msr} -> {val}\n")

                # Run turbostat
                log.write("\nTurbostat output:\n")
                full_turbostat_cmd = f"cd {TURBOSTAT_PATH}; {TURBOSTAT_CMD}"
                rc, out, err = run_ssh_command(ssh, full_turbostat_cmd, timeout=180)
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

                ssh.close()

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
