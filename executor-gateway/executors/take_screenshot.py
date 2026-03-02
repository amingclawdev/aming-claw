import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional


def safe_name(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "-", text)[:60]


def wants_all_screens(command_text: str) -> bool:
    low = (command_text or "").lower()
    keywords = [
        "all screens",
        "all monitors",
        "multi screen",
        "multiple screens",
        "双屏",
        "多屏",
        "所有屏幕",
        "全部屏幕",
    ]
    return any(k in low for k in keywords)


def resolve_output_dir() -> tuple[Path, Optional[Path]]:
    shared_raw = os.getenv("SHARED_VOLUME_PATH", "").strip()
    if shared_raw:
        shared_root = Path(shared_raw)
        out = shared_root / "screenshots"
        out.mkdir(parents=True, exist_ok=True)
        return out, shared_root

    workspace = Path(os.getenv("WORKSPACE_PATH", "/workspace"))
    out = workspace / ".openclaw" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    return out, None


def capture_windows_screens(task_id: str, out_dir: Path, all_screens: bool) -> list[Path]:
    # Uses Windows Forms APIs through PowerShell to capture each monitor.
    escaped_out_dir = str(out_dir).replace("\\", "\\\\")
    mode_line = "$screens = [System.Windows.Forms.Screen]::AllScreens" if all_screens else "$screens = @([System.Windows.Forms.Screen]::PrimaryScreen)"
    normalize_dpi = os.getenv("SCREENSHOT_NORMALIZE_DPI", "1").strip().lower() not in {"0", "false", "no"}
    ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class DpiCompat {{
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
  [DllImport("Shcore.dll")] public static extern int SetProcessDpiAwareness(int awareness);
}}
"@

try {{
  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
  [void][DpiCompat]::SetProcessDpiAwarenessContext([IntPtr](-4))
}} catch {{}}
try {{
  # PROCESS_PER_MONITOR_DPI_AWARE = 2
  [void][DpiCompat]::SetProcessDpiAwareness(2)
}} catch {{}}
try {{
  [void][DpiCompat]::SetProcessDPIAware()
}} catch {{}}

$outDir = "{escaped_out_dir}"
[System.IO.Directory]::CreateDirectory($outDir) | Out-Null
{mode_line}
$count = 0
$i = 0
foreach ($screen in $screens) {{
  $bounds = $screen.Bounds
  $gDpi = [System.Drawing.Graphics]::FromHwnd([IntPtr]::Zero)
  $scale = $gDpi.DpiX / 96.0
  $gDpi.Dispose()
  if ($scale -lt 1.0) {{ $scale = 1.0 }}

  $bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
  $g.Dispose()

  $finalBmp = $bmp
  if ({str(normalize_dpi).lower()} -and $scale -gt 1.01) {{
    $targetW = [Math]::Max(1, [int]([Math]::Round($bounds.Width / $scale)))
    $targetH = [Math]::Max(1, [int]([Math]::Round($bounds.Height / $scale)))
    $resized = New-Object System.Drawing.Bitmap($targetW, $targetH)
    $g2 = [System.Drawing.Graphics]::FromImage($resized)
    $g2.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g2.DrawImage($bmp, 0, 0, $targetW, $targetH)
    $g2.Dispose()
    $bmp.Dispose()
    $finalBmp = $resized
  }}

  $file = Join-Path $outDir ("{safe_name(task_id)}-screen-" + $i + ".jpg")
  $finalBmp.Save($file, [System.Drawing.Imaging.ImageFormat]::Jpeg)
  $finalBmp.Dispose()
  Write-Output $file
  $count++
  $i++
}}
if ($count -eq 0) {{
  Write-Error "no active screens detected in current session"
  exit 2
}}
"""
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps_script,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "powershell screenshot failed")

    candidates = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    files = [Path(p) for p in candidates if p.lower().endswith(".jpg") and Path(p).exists()]
    files = sorted(files)
    if not files:
        raise RuntimeError(
            "no screenshots generated; stdout="
            + (proc.stdout or "").strip()[:400]
            + "; stderr="
            + (proc.stderr or "").strip()[:400]
        )
    return files


def main() -> int:
    t0 = time.perf_counter()
    payload = json.load(sys.stdin)
    task_id = payload.get("task_id", "task-unknown")
    command_text = (payload.get("command_text") or "").strip()
    out_dir, shared_root = resolve_output_dir()

    if os.name != "nt":
        # In Docker/Linux this action cannot access the Windows desktop.
        result = {
            "ok": False,
            "error": "take_screenshot requires executor running on Windows host (not Linux container)",
        }
        print(json.dumps(result, ensure_ascii=True))
        return 0

    capture_start = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        temp_dir = Path(tmp)
        shots = capture_windows_screens(
            task_id=task_id,
            out_dir=temp_dir,
            all_screens=wants_all_screens(command_text),
        )
        capture_end = time.perf_counter()

        copy_start = time.perf_counter()
        final_paths = []
        for shot in shots:
            dest = out_dir / shot.name
            dest.write_bytes(shot.read_bytes())
            final_paths.append(dest)
        copy_end = time.perf_counter()

    container_files = None
    if shared_root is not None:
        container_files = [f"/openclaw-home/.openclaw/workspace/screenshots/{p.name}" for p in final_paths]

    result = {
        "ok": True,
        "summary": f"captured {len(final_paths)} screen(s)",
        "details": {
            "files": [str(p) for p in final_paths],
            "container_files": container_files,
            "telegram_sent": None,
            "telegram_via": "openclaw_dispatcher",
            "timings_ms": {
                "total_ms": int((time.perf_counter() - t0) * 1000),
                "capture_ms": int((capture_end - capture_start) * 1000),
                "copy_ms": int((copy_end - copy_start) * 1000),
            },
        },
    }
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
