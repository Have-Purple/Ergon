# Security Policy

## The boundary, stated plainly

Ergon starts processes on the machine it runs on. Everything that limits what a caller can do is a
deployment setting, and each one is a real boundary:

* **`ERGON_ALLOW`** lists the programs `argv[0]` may name. It is the capability boundary. Adding a
  shell to it removes the no-shell posture, and adding a docker client plus a mounted socket hands a
  caller root-equivalent control of the host.
* **The workspace mount** is where jobs run. A `cwd` that resolves outside it is refused, so the mount
  list is the file-access list. Mount read-write only what jobs must write.
* **`ERGON_PASS_ENV`** names the environment variables a job inherits, and the server keeps a deny-list
  for loader and interpreter variables (`LD_PRELOAD`, `LD_AUDIT`, `PYTHONSTARTUP`, `BASH_ENV` and
  friends) so a job cannot be steered through the environment.
* **The bearer token** is the only thing between a caller and the allow-list. Publish the port on the
  interfaces that need it, and keep it off the public internet.
* **The state volume** stores every job's argv at mode `0600`. A secret placed in a command line is at
  rest in that file, so pass secrets by file path, not by argument, and treat `/state` as sensitive.

## Reporting a vulnerability

Open a private report through the repository's security advisory mechanism rather than a public issue.
Include the version from `GET /health`, the deployment settings in play (`ERGON_ALLOW`, the mounts, the
published port), and the shortest request that reproduces the behavior. Reports that show a way to run
a program off the allow-list, to escape the workspace, to bypass the token, or to make the runner write
outside its bounds are treated as vulnerabilities.

## Things this project will not do

It will not run a general shell, will not accept a command line string, will not start with an empty
token, will not prune younger than its own retention floor, and will not start a job when free space on
the state volume is under the configured floor. A change that weakens any of those is a regression,
with reasons in the code comments explaining the incident that produced the rule.
