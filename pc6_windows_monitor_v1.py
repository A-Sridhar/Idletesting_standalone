import re
import threading
import time
from datetime import datetime, timedelta

import winrm


SOCWATCH_DIR = r"c:\apps\PI_PM\socwatch\64"
SOCWATCH_EXE = "socwatch.exe"
SOCWATCH_EXE_PATH = SOCWATCH_DIR + "\\" + SOCWATCH_EXE
SOCWATCH_ARGS = "-m -f cpu-cstate -f cpu-pstate"
DEFAULT_USERNAME = "administrator"
DEFAULT_PASSWORD = "intel@123"
WINRM_PORT = 5985  # default WinRM HTTP port
WINRM_READ_TIMEOUT_SEC = 90
WINRM_OPERATION_TIMEOUT_SEC = 60

PC_STATES = ("PC0", "PC1", "PC2", "PC3", "PC4", "PC5", "PC6", "PC7")
DATA_WRITTEN_RE = re.compile(r"Data written to:\s*(.+)", re.IGNORECASE)


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect_winrm(ip_address, username=DEFAULT_USERNAME, password=DEFAULT_PASSWORD):
    endpoint = f"http://{ip_address}:{WINRM_PORT}/wsman"
    return winrm.Session(
        endpoint,
        auth=(username, password),
        transport="ntlm",
        server_cert_validation="ignore",
        read_timeout_sec=WINRM_READ_TIMEOUT_SEC,
        operation_timeout_sec=WINRM_OPERATION_TIMEOUT_SEC,
    )


def run_remote_command(session, args, executable="cmd.exe"):
    """Run a command on the remote Windows host via WinRM and return (rc, stdout, stderr)."""
    result = session.run_cmd(executable, args)
    stdout = result.std_out.decode(errors="replace") if isinstance(result.std_out, bytes) else result.std_out
    stderr = result.std_err.decode(errors="replace") if isinstance(result.std_err, bytes) else result.std_err
    return result.status_code, stdout, stderr


PROGRESS_PRINT_INTERVAL_SEC = 15


def run_remote_command_with_countdown(session, args, executable, expected_seconds, label="Capture in progress"):
    """
    Run a (long) remote command in a background thread while printing progress on its own
    line every PROGRESS_PRINT_INTERVAL_SEC seconds (not a carriage-return overwrite, so it
    stays visible in plain cmd.exe scrollback/copy-paste and in redirected/log output).
    """
    outcome = {}

    def _worker():
        outcome["rc"], outcome["out"], outcome["err"] = run_remote_command(session, args, executable)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread_start = time.time()
    last_print = 0.0

    # Poll every 1s (not join(timeout=PROGRESS_PRINT_INTERVAL_SEC)) so Ctrl+C is noticed
    # promptly instead of being stuck in one long blocking join() call on Windows.
    while thread.is_alive():
        elapsed = time.time() - thread_start
        if elapsed - last_print >= PROGRESS_PRINT_INTERVAL_SEC or last_print == 0.0:
            remaining = max(0, expected_seconds - elapsed)
            mins, secs = divmod(int(remaining), 60)
            hours, mins = divmod(mins, 60)
            print(f"{label}: {hours:02d}:{mins:02d}:{secs:02d} remaining...", flush=True)
            last_print = elapsed
        thread.join(timeout=1)

    return outcome["rc"], outcome["out"], outcome["err"]


def extract_output_csv_path(socwatch_output):
    """Parse socwatch's console output for the 'Data written to: <path>' line and return the .csv path."""
    match = DATA_WRITTEN_RE.search(socwatch_output)
    if not match:
        return None
    base_path = match.group(1).strip().strip("*").strip()
    if not base_path.lower().endswith(".csv"):
        base_path += ".csv"
    return base_path


def parse_socwatch_csv(csv_text):
    """
    Parse a SoCWatch output CSV for the package C-state residency (%) values
    (PC0..PC7) and the first-instance Core C6 residency (%) (CC6).
    Returns a dict, e.g. {"PC0": "31.47", ..., "CC6": "97.89"}.
    """
    lines = csv_text.splitlines()
    results = {}

    pkg_section_idx = None
    core_section_idx = None
    for i, line in enumerate(lines):
        if "Package C-State Summary: Residency (Percentage and Time)" in line:
            pkg_section_idx = i
        elif "Core C-State Summary: Residency (Percentage and Time)" in line:
            core_section_idx = i

    if pkg_section_idx is not None:
        found_any = False
        for line in lines[pkg_section_idx + 1:]:
            cols = [c.strip() for c in line.split(",")]
            if cols and cols[0] in PC_STATES and len(cols) > 1:
                results[cols[0]] = cols[1]
                found_any = True
            elif found_any and not line.strip():
                break  # end of the PC-state table

    if core_section_idx is not None:
        for line in lines[core_section_idx + 1:]:
            cols = [c.strip() for c in line.split(",")]
            if cols and cols[0] == "CC6" and len(cols) > 1:
                results["CC6"] = cols[1]  # first-instance (Core_0) value
                break

    return results


def main():
    ip_address = input("Enter the target Windows machine IP address: ").strip()
    while not ip_address:
        ip_address = input("IP address is required. Enter the target Windows machine IP address: ").strip()

    while True:
        raw_minutes = input("Enter idle monitoring duration per loop (minutes): ").strip()
        try:
            idle_minutes = float(raw_minutes)
            if idle_minutes > 0:
                break
        except ValueError:
            pass
        print("Please enter a positive number of minutes.")

    idle_seconds = int(idle_minutes * 60)
    loop_interval_seconds = idle_seconds

    start_time = datetime.now()
    loop_count = 0
    safe_ip = ip_address.replace(".", "_")
    log_name = f"socwatch_windows_monitor_{safe_ip}_{start_time.strftime('%Y%m%d_%H%M%S')}.log"

    print(f"Logging to: {log_name}")
    print("Press Ctrl+C to stop.\n")

    with open(log_name, "a", encoding="utf-8") as log:
        log.write(f"[{now_str()}] Script started for IP={ip_address}\n")
        log.write(f"[{now_str()}] SoCWatch duration per loop={idle_seconds} seconds\n\n")

        while True:
            loop_start = datetime.now()
            loop_count += 1
            elapsed = loop_start - start_time
            pc_values = {}

            try:
                session = connect_winrm(ip_address)

                log.write(f"========== LOOP {loop_count} @ {now_str()} ==========\n")
                log.write(f"Elapsed: {str(elapsed).split('.')[0]}\n")

                # Invoke the exe by its full path directly - avoids relying on "cd ... && ..."
                # being interpreted by cmd, which WinRM's argument quoting can break.
                cmd_args = SOCWATCH_ARGS.split() + ["-t", str(idle_seconds)]
                rc, out, err = run_remote_command_with_countdown(
                    session, cmd_args, SOCWATCH_EXE_PATH, idle_seconds, label=f"Loop {loop_count} capture"
                )

                log.write("\nSoCWatch output:\n")
                log.write(out + "\n")
                if err.strip():
                    log.write(f"[stderr] {err.strip()}\n")

                if rc != 0:
                    warn_msg = f"[WARNING] socwatch.exe failed with rc={rc}"
                    print(f"\n*** {warn_msg} ***")
                    if out.strip():
                        print(out.strip())
                    if err.strip():
                        print(f"[stderr] {err.strip()}")
                    log.write(f"\n{warn_msg}\n")
                else:
                    csv_path = extract_output_csv_path(out)
                    if not csv_path:
                        warn_msg = "[WARNING] Could not find 'Data written to:' path in socwatch output"
                        print(f"\n*** {warn_msg} ***")
                        log.write(f"\n{warn_msg}\n")
                    else:
                        log.write(f"\nOutput CSV: {csv_path}\n")
                        read_rc, csv_text, read_err = run_remote_command(session, ["/c", "type", csv_path])
                        if read_rc != 0 or not csv_text.strip():
                            warn_msg = f"[WARNING] Could not read output CSV: {csv_path} ({read_err.strip()})"
                            print(f"\n*** {warn_msg} ***")
                            if read_err.strip():
                                print(f"[stderr] {read_err.strip()}")
                            log.write(f"\n{warn_msg}\n")
                        else:
                            pc_values = parse_socwatch_csv(csv_text)
                            log.write(f"\nParsed values: {pc_values}\n")

                log.write("===============================================\n\n")
                log.flush()

            except Exception as e:
                log.write(f"[{now_str()}] LOOP {loop_count} ERROR: {e}\n\n")
                log.flush()
                print(f"\n*** [WARNING] Loop {loop_count} failed: {e} ***")

            elapsed_now = datetime.now() - start_time
            elapsed_text = str(elapsed_now).split(".")[0]
            pc_display = " | ".join(
                f"{state}={pc_values.get(state, 'N/A')}" for state in ("PC0", "PC2", "PC3", "PC6", "PC7")
            )
            cc6_display = pc_values.get("CC6", "N/A")

            print(f"Loop={loop_count} | Elapsed={elapsed_text} | {pc_display} | CC6={cc6_display}")

            # Sleep until the next loop boundary relative to this loop's start
            next_run = loop_start + timedelta(seconds=loop_interval_seconds)
            sleep_seconds = max(0, (next_run - datetime.now()).total_seconds())
            while sleep_seconds > 0:
                remaining = int(sleep_seconds)
                mins, secs = divmod(remaining, 60)
                hours, mins = divmod(mins, 60)
                print(f"Next loop in {hours:02d}:{mins:02d}:{secs:02d}", end="\r", flush=True)
                time.sleep(1)
                sleep_seconds = max(0, (next_run - datetime.now()).total_seconds())
            print(" " * 50, end="\r", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting.")
