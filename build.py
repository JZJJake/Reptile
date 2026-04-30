import os
import subprocess
import shutil

def build_exe():
    print("Starting the build process for Windows Executable...")

    # 1. Clean previous builds
    if os.path.exists("build"):
        shutil.rmtree("build")
    if os.path.exists("dist"):
        shutil.rmtree("dist")

    # 2. Run PyInstaller
    # We use --add-data to include static files and templates.
    # Note: On Windows, the separator for --add-data is ';' instead of ':'
    print("Running PyInstaller...")

    # Using pyinstaller executable
    separator = ';' if os.name == 'nt' else ':'

    command = [
        "pyinstaller",
        "--name", "WebScraperClient",
        "--noconfirm",
        "--onedir", # Using onedir instead of onefile because Playwright and fastAPI don't always play nice with onefile
        "--add-data", f"static{separator}static",
        "--collect-all", "uvicorn",
        "--collect-all", "playwright",
        "--collect-data", "playwright_stealth",
        "main.py"
    ]

    subprocess.run(command, check=True)

    print("\n" + "="*50)
    print("Build complete!")
    print("Note on Playwright Browsers:")
    print("Because Playwright needs browser binaries (Chromium), you need to make sure ")
    print("they are either packaged or installed on the target machine.")
    print("\nFor the easiest deployment on Windows Server 2023:")
    print("1. Zip the 'dist/WebScraperClient' folder.")
    print("2. Copy it to the server.")
    print("3. On the server, run `WebScraperClient.exe`.")
    print("4. The first time it runs, it may need to download Chromium if not present,")
    print("   or you can run `playwright install chromium` on the server.")
    print("="*50)

if __name__ == "__main__":
    build_exe()
