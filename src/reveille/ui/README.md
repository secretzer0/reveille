# The served UI, as files

One tree, split by OWNER -- each service serves only its own subtree:

- `bus/index.html` -- the bus page. Served by the broker at `/ui` (the proxy
  mounts it at `/`).
- `launcher/index.html` -- the agents page. Served by `reveille-launch serve`
  at `/ui` (the proxy mounts it at `/agents`).

Plain HTML/CSS/JS, no build step (ruling 8635): the served bytes ARE the
source bytes, so the image tag names exactly what is served. The framework
question is deliberately open until after this extraction (msg 8637); if it
is ever reopened, it is a design of its own.

Live editing: start the service with `REVEILLE_UI_PATH=<dir>` pointing at a
directory holding your copy of `index.html`. Files are read per request --
edit, refresh, done, no container rebuild. The override ANNOUNCES itself
(`/version`, the boot banner, a visible marker on the page): a deployment
must always be able to answer "which UI am I serving".

Two comment placeholders in the bus page (`<!--NAVLINK-->`,
`<!--AGENTSNAV-->`) are replaced at serve time from env; everything else is
served verbatim.
