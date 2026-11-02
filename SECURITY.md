# Security and deployment scope

KB22 Remote is intended for a trusted local network. It serves HTTP and uses a random bearer token for API access; it is not an Internet-facing service. Do not forward its port to the public Internet. The host Mac must remain awake and connected to the KB22.

The access token is stored in `data/access-token` with mode `0600` and printed in the startup URL. The browser stores it locally after opening the URL. Keep the complete URL private; terminal transcripts and redirected startup logs may include it. Runtime presets and diagnostic logs are local data and are excluded from version control.

To replace a leaked token, stop the service, remove only `data/access-token`, restart, and reconnect clients using the newly printed URL. Existing clients will lose API access until updated. Keep presets and runtime files if you want to retain them. `--no-auth` is restricted to loopback for development.

For a sensitive security report, use this repository's GitHub private vulnerability reporting feature. Do not post live tokens, complete startup URLs, personal presets, raw logs, or audio recordings in public issues. Include a minimal reproduction, affected version, and sanitized examples. Ordinary usability and compatibility bugs can be reported as public issues.
