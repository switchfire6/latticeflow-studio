# LatticeFlow Studio in Codespaces

The studio starts by itself once the codespace is ready. **Open port 5173** in the **Ports** tab if it didn't open in a new browser tab. The backend loads MACE-MP-0 small and runs a warm-up first, so the studio can take up to a minute to connect.

- **CPU only.** A codespace has no GPU. Small cells (the presets, up to a few dozen atoms) are quick; phonons of larger supercells and long MD runs are slow on 2 cores. Bigger machine types (Codespace → *Change machine type*) help.
- **Private.** Forwarded ports are private to your GitHub account by default. The backend has no authentication, so **don't make port 8000 or 5173 public**.
- **Materials Project.** The hull, MP search/import, prototype search and defect chemical potentials need a key. Add `MP_API_KEY` as a [Codespaces secret](https://github.com/settings/codespaces) for this repository, then rebuild or restart the codespace. Without it those features report themselves unavailable.
- **Your data.** The project library lives in `~/.latticeflow` inside the codespace and is deleted with it. Use *Export project* to keep your work.

## Commands

```bash
bash .devcontainer/start.sh restart       # restart both servers
bash .devcontainer/start.sh restart dev   # frontend with hot reload (for editing the frontend)
tail -f /tmp/latticeflow-backend.log      # backend log
tail -f /tmp/latticeflow-frontend.log     # frontend log
source ~/.venvs/latticeflow/bin/activate  # the backend's Python (tests: cd backend && pytest -q)
```
