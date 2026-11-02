Valtrion runs a test or an app check as a supervised job with a time limit, hang detection, a clear result and saved logs. When a check hangs, fails halfway or runs on another machine, it keeps the status, events and console output, so you can see what actually ran.

## How it works

- You give Valtrion a command: your tests, a data check or a Unity plan.
- It runs the command on this machine or on an SSH host you configured, with limits you set.
- It ends the job with one result, PASS, FAIL, TIMEOUT or STALL when there was no progress for too long, and writes job.json, events.jsonl and console.log.

Valtrion works with any command. For Unity apps it also runs test plans inside the app and collects screenshots and metrics. The command-line tool needs Python 3.10 or newer and has no other dependencies.
