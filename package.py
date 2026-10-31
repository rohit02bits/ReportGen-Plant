import os
import sys
import shutil
import subprocess

def clean_build_dirs():
    """Cleans up previous build artifacts."""
    dirs_to_clean = ["build", "dist"]
    files_to_clean = ["ReportGenerator.spec", "ReportGeneratorService.spec"]
    
    for d in dirs_to_clean:
        if os.path.exists(d):
            print(f"Cleaning directory: {d}...")
            try:
                shutil.rmtree(d)
            except Exception as e:
                print(f"Error cleaning directory {d}: {e}")
                
    for f in files_to_clean:
        if os.path.exists(f):
            print(f"Cleaning file: {f}...")
            try:
                os.remove(f)
            except Exception as e:
                print(f"Error cleaning file {f}: {e}")

def build_executable(cmd):
    """Runs a single PyInstaller build command, installing PyInstaller if missing."""
    print(f"Executing: {' '.join(cmd)}")
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as e:
        print(f"Error during PyInstaller build: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("Error: PyInstaller not found. Installing via pip first...", file=sys.stderr)
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
            subprocess.check_call(cmd)
        except Exception as ex:
            print(f"Failed to install or run PyInstaller: {ex}", file=sys.stderr)
            sys.exit(1)

def run_build():
    """Runs the PyInstaller build commands for the CLI and the Windows Service."""
    print("Starting PyInstaller packaging...")

    # Main CLI executable
    build_executable([
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name=ReportGenerator",
        "--clean",
        "main.py"
    ])

    # Windows Service executable (win32timezone is imported dynamically by pywin32)
    if sys.platform == "win32":
        build_executable([
            sys.executable,
            "-m",
            "PyInstaller",
            "--onefile",
            "--name=ReportGeneratorService",
            "--hidden-import=win32timezone",
            "--clean",
            "windows_service.py"
        ])

    print("Executables successfully created in 'dist' directory.")

if __name__ == "__main__":
    clean_build_dirs()
    run_build()
