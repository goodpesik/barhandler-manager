<!-- PR into `production` cuts a release; PR into `main` doesn't. -->

## What changed

<!-- one-paragraph summary -->

## Release (only if PR targets `production`)

- [ ] `VERSION` bumped (semver: patch for bugfix, minor for new endpoint, major for breaking)
- [ ] No new external dependency without thinking about Windows / Android / Linux install impact
- [ ] Installer scripts (`installers/install.sh` / `install.ps1` / `install-android.sh`) updated if the install flow changed
- [ ] `README.md` updated if endpoints / install steps / config keys changed

## Test plan

- [ ] `python main.py` boots locally
- [ ] `curl http://localhost:9999/health` returns 200
- [ ] Hardware-touching changes were verified on a real printer
