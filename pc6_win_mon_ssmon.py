import os
import re
import threading
import time
from datetime import datetime

import winrm


SSMON_DIR = r"c:\apps\PI_PM\ssmd_win"
SSMON_EXE = "ssmon.exe"
SSMON_EXE_PATH = SSMON_DIR + "\\" + SSMON_EXE
SSMON_CONFIG_PATH = SSMON_DIR + "\\configs\\ssmonConfigCState.json"
SSMON_ARGS = [
    "-p", "1", "-d", "2", "-i", "10000", "-u", "10000",
    "-l", "1", "-y", "2", "-x", "C6_PC6_Cstate_Config",
    "-f", SSMON_CONFIG_PATH,
]

DEFAULT_USERNAME = "administrator"
DEFAULT_PASSWORD = "intel@123"
WINRM_PORT = 5985  # default WinRM HTTP port
WINRM_READ_TIMEOUT_SEC = 90
WINRM_OPERATION_TIMEOUT_SEC = 60
LOOP_FAILURE_BACKOFF_SEC = 15  # pause after any loop failure to avoid hammering the target


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
    """Run a PowerShell script remotely and return (rc, stdout, stderr)."""
    result = session.run_ps(script)
    stdout = result.std_out.decode(errors="replace") if isinstance(result.std_out, bytes) else result.std_out
    stderr = result.std_err.decode(errors="replace") if isinstance(result.std_err, bytes) else result.std_err
    return result.status_code, stdout, stderr


def stop_remote_ssmon(session):
    """Best-effort remote equivalent of Ctrl+C: force-stop ssmon.exe on the target host."""
    try:
        return run_remote_command(session, ["/c", "taskkill", "/IM", SSMON_EXE, "/F"])
    except Exception as e:
        return -1, "", str(e)


def build_capture_script(loop_count, duration_seconds):
    """
    Build a PowerShell script that starts ssmon.exe (with its own working directory so the
    relative config path also resolves), waits exactly duration_seconds, force-stops it if
    still running, then prints its captured stdout/stderr wrapped in markers.
    """
    stdout_file = f"{SSMON_DIR}\\ssmon_stdout_{loop_count}.log"
    stderr_file = f"{SSMON_DIR}\\ssmon_stderr_{loop_count}.log"
    arg_list = ",".join(f"'{a}'" for a in SSMON_ARGS)
    return f"""
Remove-Item -Path '{stdout_file}','{stderr_file}' -ErrorAction SilentlyContinue
$proc = Start-Process -FilePath '{SSMON_EXE_PATH}' -ArgumentList @({arg_list}) `
    -WorkingDirectory '{SSMON_DIR}' -RedirectStandardOutput '{stdout_file}' `
    -RedirectStandardError '{stderr_file}' -PassThru -NoNewWindow
Start-Sleep -Seconds {duration_seconds}
if (-not $proc.HasExited) {{
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
}}
Start-Sleep -Milliseconds 500
Write-Output "===STDOUT_START==="
if (Test-Path '{stdout_file}') {{ Get-Content -Path '{stdout_file}' -Raw }}
Write-Output "===STDOUT_END==="
Write-Output "===STDERR_START==="
if (Test-Path '{stderr_file}') {{ Get-Content -Path '{stderr_file}' -Raw }}
Write-Output "===STDERR_END==="
"""


def split_capture_output(combined_output):
    """Split the marker-wrapped combined output back into (stdout_text, stderr_text)."""
    stdout_match = re.search(r"===STDOUT_START===(.*)===STDOUT_END===", combined_output, re.DOTALL)
    stderr_match = re.search(r"===STDERR_START===(.*)===STDERR_END===", combined_output, re.DOTALL)
    stdout_text = stdout_match.group(1).strip() if stdout_match else ""
    stderr_text = stderr_match.group(1).strip() if stderr_match else ""
    return stdout_text, stderr_text


def run_capture_with_countdown(session, loop_count, duration_seconds, label="Capture in progress"):
    """Run one ssmon capture in a background thread while updating a single countdown line."""
    outcome = {}

    def _worker():
        try:
            script = build_capture_script(loop_count, duration_seconds)
            outcome["rc"], outcome["out"], outcome["err"] = run_remote_powershell(session, script)
        except Exception as e:
            outcome["exc"] = e

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread_start = time.time()

    try:
        while thread.is_alive():
            elapsed = time.time() - thread_start
            remaining = max(0, duration_seconds - elapsed)
            mins, secs = divmod(int(remaining), 60)
            hours, mins = divmod(mins, 60)
            print(f"{label}: {hours:02d}:{mins:02d}:{secs:02d} remaining...", end="\r", flush=True)
            thread.join(timeout=1)
    except KeyboardInterrupt:
        print("\n\n*** Ctrl+C received - stopping ssmon.exe on the remote host... ***")
        kill_rc, kill_out, kill_err = stop_remote_ssmon(session)
        if kill_rc == 0:
            print("Remote ssmon.exe stopped.")
        else:
            print(f"[WARNING] Could not confirm remote stop (rc={kill_rc}). {(kill_err or kill_out).strip()}")
        raise

    print(" " * 60, end="\r", flush=True)
    if "exc" in outcome:
        raise outcome["exc"]
    return outcome["rc"], outcome["out"], outcome["err"]


ANSI_ESCAPE_RE = re.compile(r"\x1b?\[[0-9;]*m")


def parse_ssmon_best(ssmon_stdout):
    """
    Parse ssmon's table output for the CPU/package-aggregate row of each Index (the row where
    Cor='-' and Thr='-' - the first row printed for that index) and return C6/PC2/PC6 from
    whichever such row has the highest PC6 (deepest idle), all three taken from that same row.
    Returns {} if nothing could be parsed.
    """
    clean_stdout = ANSI_ESCAPE_RE.sub("", ssmon_stdout)
    lines = [ln.rstrip() for ln in clean_stdout.splitlines() if ln.strip()]

    header_idx = None
    for i, ln in enumerate(lines):
        low = ln.lower()
        if "index" in low and "c6" in low and "pc6" in low:
            header_idx = i
            break
    if header_idx is None:
        return {}

    headers = re.split(r"\s+", lines[header_idx].strip())
    lower_headers = [h.lower() for h in headers]
    try:
        idx_cor = lower_headers.index("cor")
        idx_thr = lower_headers.index("thr")
        idx_c6 = lower_headers.index("c6")
        idx_pc2 = lower_headers.index("pc2")
        idx_pc6 = lower_headers.index("pc6")
    except ValueError:
        return {}

    max_col = max(idx_cor, idx_thr, idx_c6, idx_pc2, idx_pc6)
    best = {}
    best_pc6 = None

    for line in lines[header_idx + 1:]:
        cols = re.split(r"\s+", line.strip())
        if len(cols) <= max_col:
            continue
        if cols[idx_cor] != "-" or cols[idx_thr] != "-":
            continue  # only the package/aggregate row - first row of each index
        try:
            pc6_val = float(cols[idx_pc6])
            float(cols[idx_pc2])
            float(cols[idx_c6])
        except ValueError:
            continue
        if best_pc6 is None or pc6_val > best_pc6:
            best_pc6 = pc6_val
            best = {"C6": cols[idx_c6], "PC2": cols[idx_pc2], "PC6": cols[idx_pc6]}

    return best


def main():
    ip_address = input("Enter the target Windows machine IP address: ").strip()
    while not ip_address:
        ip_address = input("IP address is required. Enter the target Windows machine IP address: ").strip()

    while True:
        raw_minutes = input("Enter monitor duration per loop (minutes): ").strip()
        try:
            duration_minutes = float(raw_minutes)
            if duration_minutes > 0:
                break
        except ValueError:
            pass
        print("Please enter a positive number of minutes.")

    capture_duration_sec = int(duration_minutes * 60)
    WARMUP_SECONDS = 60

    start_time = datetime.now()
    loop_count = 0
    safe_ip = ip_address.replace(".", "_")
    run_stamp = start_time.strftime("%Y%m%d_%H%M%S")
    log_name = f"ssmon_windows_monitor_{safe_ip}_{run_stamp}.log"

    output_dir = os.path.abspath(f"ssmon_logs_{safe_ip}_{run_stamp}")
    os.makedirs(output_dir, exist_ok=True)

    print(f"Logging to: {log_name}")
    print(f"Per-loop ssmon logs will be saved to: {output_dir}")
    print("Press Ctrl+C to stop.\n")

    with open(log_name, "a", encoding="utf-8") as log:
        log.write(f"[{now_str()}] Script started for IP={ip_address}\n")
        log.write(f"[{now_str()}] Capture duration per loop={capture_duration_sec} seconds\n\n")

        while True:
            loop_start = datetime.now()
            elapsed = loop_start - start_time
            best = {}
            current_duration = WARMUP_SECONDS if loop_count == 0 else capture_duration_sec

            try:
                session = connect_winrm(ip_address)

                log.write(f"========== LOOP {loop_count} @ {now_str()} ==========\n")
                log.write(f"Elapsed: {str(elapsed).split('.')[0]}\n")
                log.write(f"Duration: {current_duration} seconds{' (warm-up)' if loop_count == 0 else ''}\n")

                rc, combined_out, ps_err = run_capture_with_countdown(
                    session, loop_count, current_duration, label=f"Loop {loop_count} capture"
                )
                stdout_text, stderr_text = split_capture_output(combined_out)

                log.write("\nssmon stdout:\n")
                log.write(stdout_text + "\n")
                if stderr_text.strip():
                    log.write(f"\nssmon stderr:\n{stderr_text}\n")
                if ps_err.strip():
                    log.write(f"[powershell stderr] {ps_err.strip()}\n")

                if not stdout_text.strip():
                    warn_msg = "[WARNING] No ssmon output captured"
                    print(f"\n*** {warn_msg} ***")
                    log.write(f"\n{warn_msg}\n")
                else:
                    local_log_name = f"loop{loop_count:03d}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
                    local_log_path = os.path.join(output_dir, local_log_name)
                    with open(local_log_path, "w", encoding="utf-8") as out_file:
                        out_file.write(stdout_text)
                    print(f"Saved: {local_log_path}")
                    log.write(f"\nSaved local copy: {local_log_path}\n")

                    best = parse_ssmon_best(stdout_text)
                    if not best:
                        warn_msg = "[WARNING] Could not find a valid C6/PC2/PC6 row in ssmon output"
                        print(f"\n*** {warn_msg} ***")
                        log.write(f"\n{warn_msg}\n")
                    else:
                        log.write(f"\nBest values: {best}\n")

                log.write("===============================================\n\n")
                log.flush()

            except Exception as e:
                log.write(f"[{now_str()}] LOOP {loop_count} ERROR: {e}\n\n")
                log.flush()
                print(f"\n*** [WARNING] Loop {loop_count} failed: {e} ***")
                # Mandatory pause on failure - prevents a runaway loop from hammering
                # the target with rapid-fire connection attempts if something's wrong.
                time.sleep(LOOP_FAILURE_BACKOFF_SEC)

            elapsed_now = datetime.now() - start_time
            elapsed_text = str(elapsed_now).split(".")[0]
            print(
                f"Loop={loop_count} | Elapsed={elapsed_text} | "
                f"C6={best.get('C6', 'N/A')} | PC2={best.get('PC2', 'N/A')} | PC6={best.get('PC6', 'N/A')}"
            )

            loop_count += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting.")
