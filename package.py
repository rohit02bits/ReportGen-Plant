import os
import sys
import shutil
import subprocess

def clean_build_dirs():
    """Cleans up previous build artifacts."""
    dirs_to_clean = ["build", "dist"]
    files_to_clean = ["ReportGenerator.spec"]
    
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

def run_build():
    """Runs the PyInstaller build command."""
    print("Starting PyInstaller packaging...")
    
    # Define PyInstaller build command arguments
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name=ReportGenerator",
        "--clean",
        "main.py"
    ]
    
    print(f"Executing: {' '.join(cmd)}")
    try:
        # Run PyInstaller
        subprocess.check_call(cmd)
        print("Executable successfully created in 'dist' directory.")
    except subprocess.CalledProcessError as e:
        print(f"Error during PyInstaller build: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("Error: PyInstaller not found. Installing via pip first...", file=sys.stderr)
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
            subprocess.check_call(cmd)
            print("Executable successfully created in 'dist' directory.")
        except Exception as ex:
            print(f"Failed to install or run PyInstaller: {ex}", file=sys.stderr)
            sys.exit(1)

if __name__ == "__main__":
    clean_build_dirs()
    run_build()
