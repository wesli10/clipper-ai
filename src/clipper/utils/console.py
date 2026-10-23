"""Shared rich console. User-facing messages are in Portuguese (CLAUDE.md)."""

from __future__ import annotations

from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def info(message: str) -> None:
    console.print(message)


def warn(message: str) -> None:
    err_console.print(f"[yellow]aviso[/yellow] {message}")


def error(message: str) -> None:
    err_console.print(f"[red]erro[/red] {message}")


def success(message: str) -> None:
    console.print(f"[green]ok[/green] {message}")
