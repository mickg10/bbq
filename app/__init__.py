"""bbq — tailnet-only dashboard and API for the BBQ Guru CyberQ WiFi.

Safety invariants for this package (see DESIGN.md):
- No raw proxying of requests to the device; only allowlisted reads and
  allowlisted, validated form POSTs built by ``app.device``.
- Firmware/factory reset is never implemented; ``booting.htm``/``RESET_CPU``
  and reset-like field names are denied at the client level.
- Credential fields (SMTP_PWD, WIFI_KEY*, HTTP_PASS) are write-only: never
  logged, stored, or returned.
"""
