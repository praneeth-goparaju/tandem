"""The vault connector: an MCP server that gives the owner's claude.ai chats
read and write access to the Obsidian vault.

It runs as its own process on 127.0.0.1, behind a Cloudflare Tunnel. It is not
part of the agent fleet and shares no session with it.
"""
