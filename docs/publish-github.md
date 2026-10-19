# Repository and update workflow

Repository: [ArnavNevgi/substation-scada-simulator](https://github.com/ArnavNevgi/substation-scada-simulator)

Description:

> 115 kV substation and solar SCADA simulator using Python, pandapower, Modbus TCP, and Ignition. Includes breaker and inverter controls, alarms, historical trends, command auditing, and communication health monitoring.

Suggested topics: `scada`, `ignition`, `modbus-tcp`, `pandapower`, `power-systems`, `solar-energy`, `python`, `simulation`, `industrial-automation`.

## Get a local copy

With Git installed, open PowerShell and run:

```powershell
git clone https://github.com/ArnavNevgi/substation-scada-simulator.git
cd substation-scada-simulator
```

The root contains `README.md`, `requirements.txt`, `simulator`, `ignition`, `evidence` and `docs`. This repository contains source and evidence; Gateway configuration dependencies are described in the setup guide. The native project ZIP remains an import artifact under `ignition/` alongside the readable resources.

## Publish later changes

Start from an up-to-date local copy, create a branch, make the changes, and run the relevant checks listed in the verification guide. Review the files before committing:

```powershell
git pull --ff-only
git switch -c describe-your-change
git status --short
git diff
git add path/to/changed-file
git commit -m "Describe the resulting change"
git push -u origin describe-your-change
```

Replace the example branch and file names with the actual ones. Open a pull request into `main` and check the GitHub Actions results before merging. GitHub authentication and a local Git author identity are required for commits and pushes. The included `.gitignore` excludes virtual environments, caches, local credentials, databases and generated simulator artifacts. Keep the original captured evidence intact when adding new evidence.

For a small documentation edit, GitHub's file editor can also create a branch and pull request from the repository page.

## Review the published page

Confirm that the README displays its Overview image, its documentation links open, and the screenshot and packet evidence is accessible. The [Actions page](https://github.com/ArnavNevgi/substation-scada-simulator/actions) shows the hosted checks. Repository topics can be added through the About area; the repository can also be pinned on the author's profile.

## References

- [GitHub: cloning a repository](https://docs.github.com/en/repositories/creating-and-managing-repositories/cloning-a-repository)
- [GitHub: creating a pull request](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request)
