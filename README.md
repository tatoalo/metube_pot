# MeTube w/POT support

![Build Status](https://github.com/tatoalo/metube_pot/actions/workflows/main.yml/badge.svg)
![Docker Pulls](https://img.shields.io/docker/pulls/tatoalo/metube_pot.svg)

A fork of [MeTube](https://github.com/alexta69/metube) with **YouTube PO Token (Proof-of-Origin)** support and **Jellyfin integration**.

## Features

### POT (Proof-of-Origin) Support
Bypasses YouTube's "Sign in to confirm you're not a bot" restrictions by integrating with [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider).

### Jellyfin Integration
Automatically generates `.nfo` metadata files for downloaded videos, enabling Jellyfin to properly index and display Video title and description and other basic metadata.

## Run using Docker Compose

```yaml
services:
  metube:
    image: ghcr.io/tatoalo/metube_pot
    container_name: metube
    [...]
```
