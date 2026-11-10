# Runbook: Disk full / no space left on device

Symptoms: "No space left on device", pods evicted, services failing to write logs, database read-only.

## Diagnose
1. `df -h` to find the filesystem at or near 100%.
2. `df -i` to check for inode exhaustion (plenty of space but cannot create files).
3. Identify the biggest consumers under the full mount (human runs `du -xh --max-depth=2`).
4. Check log directories and container logs first; they are the usual culprit.

## Fix (human approval required)
- Rotate or compress old logs; do not delete unrotated logs of an active incident.
- `docker system df` to see image/volume usage; prune only with the owner's agreement.
- Kubernetes node: check for evicted pods and large emptyDir volumes.

## Escalate if
- The full volume holds database or persistent data.
- Growth rate suggests it will fill again within hours (find the source, not just the symptom).
