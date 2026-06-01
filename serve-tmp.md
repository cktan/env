---
name: serve-tmp
description: Serve files in /tmp over HTTP from the Sprite VM so they can be downloaded to a local machine. Use when the user wants to copy/download/pull a file off the sprite, get a file to their laptop, or share a generated artifact. Sprites have no scp/ssh — the only egress is the HTTP proxy.
---

You serve files over the Sprite HTTP proxy so the user can download
them to their local machine. There is no scp/ssh into a Sprite; the
proxy is the only egress.

Use the `sprite-env` CLI via Bash (not MCP Sprites tools).

## Steps

1) If the user named a specific file outside `/tmp`, copy it in
   first: `cp <path> /tmp/`.

2) Start (or restart) the serving service on port 8080:
   ```
   sprite-env services create serve-tmp --cmd python3 \
     --args "-m,http.server,8080,--directory,/tmp" --http-port 8080
   ```
   If it already exists, use `sprite-env services restart serve-tmp`.

3) Verify locally: `curl -s -o /dev/null -w "%{http_code}\n"
   http://localhost:8080/<filename>` should print `200`.

4) Get the sprite URL with `sprite-env info` (field `sprite_url`)
   and give the user the download link:
   `https://<sprite-url>/<filename>` (browser) or
   `curl -O https://<sprite-url>/<filename>` (local terminal).

## Important

- Auth mode defaults to `sprite` (org members only). Fine for the
  user downloading to their own laptop. The proxy CAN be made
  public — never serve `/tmp` if it holds secrets; move only the
  intended file into a clean dir and serve that instead.
- Only ONE service can hold `--http-port`. If another service owns
  it, ask before taking it over.
- Always remind the user to tear down when done:
  `sprite-env services delete serve-tmp`
- Offer to delete the service yourself once they confirm the
  download is complete.
