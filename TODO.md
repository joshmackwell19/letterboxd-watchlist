# To do

- **Lock the ranking (and the other Worker writes) to Josh only.** The
  dashboard is a public page and the Worker's shared secret
  (`REFRESH_TRIGGER_SECRET` in `dashboard.py`) is in its source, so anyone
  who looks can call `/rank-save` and overwrite the ranked list — or any
  other write endpoint. Fine for now; the fix is a secret that isn't
  shipped in the page: for example a passphrase entered once per device
  and kept in `localStorage`, checked by the Worker against a Cloudflare
  secret, or Cloudflare Access in front of the Worker. Until then a bad
  save can be undone by deleting the newest `ranking_state` rows (the
  Worker keeps the last 40 saves per year).
