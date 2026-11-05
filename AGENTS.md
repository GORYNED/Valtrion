# Agent instructions for Valtrion

Valtrion is open source. Everything in this repository (files, tests, examples, screenshots, commit messages, branch names, pull request
titles and texts, comments) is public. These rules apply to every agent and every person who changes it.

## Never name anything real

Do not write the name of a real project, game or app, a real device (a phone's or tablet's model, an Android serial, an iOS device id), a real
machine or host, a user, a person, an e-mail address, a private network address or a path under a real user folder. This holds even when the value
comes from the machine you work on, from a log, a screenshot, a job or a lease, and even in a test fixture, a documentation example, a changelog
entry or a commit message.

Use made-up names instead:

- projects and apps: `Game`, `MyGame`, `ExampleApp`; a working copy: `task-ab12cd`
- devices: `Example Phone 5G`, `Android phone`, `Android emulator`, `android:A1B2`; iOS ids padded with zeros (`00008000-0000000000000001`)
- machines and users: `build-pc`, `mac`, `dev`, `builder`; paths `C:\Users\dev\...`, `/Users/builder/...`, `/home/dev/...`
- addresses: `10.0.0.5`, `203.0.113.9`; e-mail: `name@example.com`

A real value that helps explain a problem stays in your conversation, not in the repository.

## Screenshots and images

Take them of made-up or masked data. Replace project, device, host, user and program names before capturing; do not crop or blur afterwards (the
text is still in the file's history if you only hide it). A person must look at every image before it is committed.

## Check before you push and before you ask for a merge

```sh
python tools/check_privacy.py            # what this branch adds to origin/main, its commit messages and its branch name
python tools/check_privacy.py --all      # every tracked text file
python tools/check_privacy.py --text-file pr.md   # a pull request title and body saved to a file
```

The script holds no private name. It finds user folders, e-mail addresses, private addresses and device ids by pattern, and it reads two lists of
names: a plain one outside the repository (`~/.valtrion/private-terms.txt`, or the file named by `VALTRION_PRIVATE_TERMS`; keep your own real
project, device and host names there) and `tools/privacy-terms.sha256`, which holds only hashes. To add a name to the shared list run
`python tools/check_privacy.py --hash "Name"` and put the line in that file; never write the name itself into the repository. Images cannot be
read by the script: it lists them so that you look at them.

If something was pushed by mistake, say so at once: a commit stays reachable by its hash after the branch is rewritten, so the owner may need to
ask the hosting service to remove it.

## Other rules

- Tests: `python -m unittest discover -s tests -t .` in `core`. New behavior needs tests. A change to the Unity package needs a run in a real Editor
  when it can be done, or the pull request says that it was not.
- Releases follow [docs/releasing.md](docs/releasing.md). Do not merge a pull request, publish a package or tag unless the owner says so.
- Keep documentation plain and short; describe what the software does, not where it was tried.
