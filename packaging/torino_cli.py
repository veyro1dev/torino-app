"""PyInstaller entry point for torino-cli.exe (CLI + MCP stdio server)."""
import multiprocessing

from torino_parts.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
