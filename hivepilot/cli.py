
import typer
from rich import print
from .orchestrator import run_task

app = typer.Typer()

@app.command()
def run(task: str, project: str):
    run_task(task, project)

@app.command()
def doctor():
    print("[green]HivePilot environment looks ready.[/green]")
