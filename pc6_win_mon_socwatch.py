import os
import re
import threading
import time
from datetime import datetime, timedelta

import winrm


SOCWATCH_DIR = r"c:\apps\PI_PM\socwatch\64"
SOCWATCH_EXE = "socwatch.exe"
SOCWATCH_EXE_PATH = SOCWATCH_DIR + "\\" + SOCWATCH_EXE
SOCWATCH_ARGS = "-m -f cpu-cstate -f cpu-pstate"
DEFAULT_OUTPUT_PREFIX = SOCWATCH_DIR + "\\SoCWatchOutput"
DEFAULT_USERNAME = "administrator"
DEFAULT_PASSWORD = "intel@123"
WINRM_PORT = 5985  # default WinRM HTTP port
WINRM_READ_TIMEOUT_SEC = 90
WINRM_OPERATION_TIMEOUT_SEC = 60
SOCWATCH_MAX_ATTEMPTS = 3
SOCWATCH_RETRY_DELAY_SEC = 8

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


def run_remote_powershell(session, script):
    """Run a PowerShell script remotely - avoids cmd's redirection/&& tokenization pitfalls."""
    result = session.run_ps(script)
    stdout = result.std_out.decode(errors="replace") if isinstance(result.std_out, bytes) else result.std_out
    stderr = result.std_err.decode(errors="replace") if isinstance(result.std_err, bytes) else result.std_err
    return result.status_code, stdout, stderr


def stop_remote_socwatch(session):
    """Best-effort remote equivalent of Ctrl+C: force-stop socwatch.exe on the target host."""
    try:
        return run_remote_command(session, ["/c", "taskkill", "/IM", SOCWATCH_EXE, "/F"])
    except Exception as e:
        return -1, "", str(e)


def run_remote_command_with_countdown(session, args, executable, expected_seconds, label="Capture in progress"):
    """Run a (long) remote command in a background thread while updating a single countdown line."""
    outcome = {}

    def _worker():
        outcome["rc"], outcome["out"], outcome["err"] = run_remote_command(session, args, executable)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread_start = time.time()

    # Poll every 1s so Ctrl+C is noticed promptly instead of being stuck in one
    # long blocking join() call on Windows.
    try:
        while thread.is_alive():
            elapsed = time.time() - thread_start
            remaining = max(0, expected_seconds - elapsed)
            mins, secs = divmod(int(remaining), 60)
            hours, mins = divmod(mins, 60)
            print(f"{label}: {hours:02d}:{mins:02d}:{secs:02d} remaining...", end="\r", flush=True)
            thread.join(timeout=1)
    except KeyboardInterrupt:
        print("\n\n*** Ctrl+C received - stopping socwatch.exe on the remote host... ***")
        kill_rc, kill_out, kill_err = stop_remote_socwatch(session)
        if kill_rc == 0:
            print("Remote socwatch.exe stopped.")
        else:
            print(f"[WARNING] Could not confirm remote stop (rc={kill_rc}). {(kill_err or kill_out).strip()}")
        raise

    print(" " * 60, end="\r", flush=True)
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
    WARMUP_SECONDS = 60

    start_time = datetime.now()
    loop_count = -1
    safe_ip = ip_address.replace(".", "_")
    run_stamp = start_time.strftime("%Y%m%d_%H%M%S")
    log_name = f"socwatch_windows_monitor_{safe_ip}_{run_stamp}.log"

    csv_output_dir = os.path.abspath(f"socwatch_csv_{safe_ip}_{run_stamp}")
    os.makedirs(csv_output_dir, exist_ok=True)

    print(f"Logging to: {log_name}")
    print(f"Per-loop SoCWatch CSVs will be saved to: {csv_output_dir}")
    print("Press Ctrl+C to stop.\n")

    with open(log_name, "a", encoding="utf-8") as log:
        log.write(f"[{now_str()}] Script started for IP={ip_address}\n")
        log.write(f"[{now_str()}] SoCWatch duration per loop={idle_seconds} seconds\n\n")

        last_csv_path = None

        while True:
            loop_start = datetime.now()
            loop_count += 1
            elapsed = loop_start - start_time
            pc_values = {}
            current_duration = WARMUP_SECONDS if loop_count == 0 else idle_seconds

            try:
                session = connect_winrm(ip_address)

                log.write(f"========== LOOP {loop_count} @ {now_str()} ==========\n")
                log.write(f"Elapsed: {str(elapsed).split('.')[0]}\n")
                log.write(f"Duration: {current_duration} seconds{' (warm-up)' if loop_count == 0 else ''}\n")

                # Delete any stale default-named output files first (from a previous loop,
                # or leftover from earlier manual/script runs) - socwatch reuses the same
                # fixed filename every run, and a leftover/locked one makes it fail with
                # "Cannot create output files". Run this unconditionally, every loop.
                run_remote_command(session, ["/c", "del", "/F", "/Q", DEFAULT_OUTPUT_PREFIX + ".*"])

                # Diagnostic: confirm the account can actually write to SOCWATCH_DIR,
                # independent of socwatch itself, and log what's currently in there.
                dir_rc, dir_out, dir_err = run_remote_command(session, ["/c", "dir", SOCWATCH_DIR])
                log.write(f"\nDirectory listing ({SOCWATCH_DIR}):\n{dir_out}\n")
                if dir_err.strip():
                    log.write(f"[dir stderr] {dir_err.strip()}\n")

                write_test_path = SOCWATCH_DIR + "\\_write_test.tmp"
                ps_script = (
                    f'try {{ Set-Content -Path "{write_test_path}" -Value "test" -ErrorAction Stop; '
                    f'Remove-Item -Path "{write_test_path}" -ErrorAction Stop; Write-Output "WRITE_OK" }} '
                    f'catch {{ Write-Output "WRITE_FAIL: $($_.Exception.Message)" }}'
                )
                wt_rc, wt_out, wt_err = run_remote_powershell(session, ps_script)
                log.write(f"Write test rc={wt_rc} out={wt_out.strip()!r} err={wt_err.strip()!r}\n")

                # A previous run force-killed via taskkill (e.g. Ctrl+C) can leave its ETW
                # trace session registered, which then blocks a new socwatch run from
                # starting its own session/output - stop any leftover ones (best-effort).
                sessions_rc, sessions_out, _ = run_remote_command(session, ["/c", "logman", "query", "-ets"])
                log.write(f"\nActive ETW sessions:\n{sessions_out}\n")
                for session_line in sessions_out.splitlines():
                    name = session_line.split()[0] if session_line.strip() else ""
                    if "socwatch" in name.lower():
                        stop_rc, stop_out, _ = run_remote_command(session, ["/c", "logman", "stop", name, "-ets"])
                        log.write(f"Stopped leftover ETW session '{name}': rc={stop_rc} {stop_out.strip()}\n")

                # Invoke the exe by its full path directly - avoids relying on "cd ... && ..."
                # being interpreted by cmd, which WinRM's argument quoting can break.
                cmd_args = SOCWATCH_ARGS.split() + ["-t", str(current_duration)]

                for attempt in range(1, SOCWATCH_MAX_ATTEMPTS + 1):
                    rc, out, err = run_remote_command_with_countdown(
                        session, cmd_args, SOCWATCH_EXE_PATH, current_duration, label=f"Loop {loop_count} capture"
                    )
                    if rc == 0 or attempt == SOCWATCH_MAX_ATTEMPTS:
                        break
                    retry_msg = f"[WARNING] socwatch.exe failed (attempt {attempt}/{SOCWATCH_MAX_ATTEMPTS}), retrying in {SOCWATCH_RETRY_DELAY_SEC}s..."
                    print(f"\n{retry_msg}")
                    log.write(f"{retry_msg}\n")
                    time.sleep(SOCWATCH_RETRY_DELAY_SEC)

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
                        last_csv_path = csv_path
                        log.write(f"\nOutput CSV: {csv_path}\n")
                        read_rc, csv_text, read_err = run_remote_command(session, ["/c", "type", csv_path])
                        if read_rc != 0 or not csv_text.strip():
                            warn_msg = f"[WARNING] Could not read output CSV: {csv_path} ({read_err.strip()})"
                            print(f"\n*** {warn_msg} ***")
                            if read_err.strip():
                                print(f"[stderr] {read_err.strip()}")
                            log.write(f"\n{warn_msg}\n")
                        else:
                            local_csv_name = f"loop{loop_count:03d}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                            local_csv_path = os.path.join(csv_output_dir, local_csv_name)
                            with open(local_csv_path, "w", encoding="utf-8") as csv_file:
                                csv_file.write(csv_text)
                            print(f"Saved: {local_csv_path}")
                            log.write(f"\nSaved local copy: {local_csv_path}\n")

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
            next_run = loop_start + timedelta(seconds=current_duration)
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
