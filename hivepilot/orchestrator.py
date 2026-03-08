
import yaml
import subprocess
from pathlib import Path

def load_yaml(file):
    with open(file) as f:
        return yaml.safe_load(f)

def run_task(task_name, project_name):
    tasks = load_yaml("tasks.yaml")["tasks"]
    projects = load_yaml("projects.yaml")["projects"]

    task = tasks[task_name]
    project = projects[project_name]

    repo_path = Path(project["path"])
    prompt_file = task["prompt"]

    with open(prompt_file) as f:
        prompt = f.read()

    subprocess.run(["claude", prompt], cwd=repo_path)
