# Upstream Sync Rebase Plan: 2026-01-10

**Upstream Release**: [2026.01.09](https://github.com/alexta69/metube/releases/tag/2026.01.09)
**Source Issue**: [#8 - Upstream Sync Analysis](https://github.com/tatoalo/metube_pot/issues/8)
**Merge Base**: `a002af9` (Merge pull request #864 dependabot/github_actions)

---

## Summary

Merge 7 upstream commits introducing queue repair functionality, documentation improvements, and two breaking configuration removals (`DOWNLOAD_MODE` and `playlist_strict_mode`).

---

## Upstream Changes

| Commit | Description |
|--------|-------------|
| `d799a4a` | feature/repair-persistent-queues |
| `191f17e` | syntax changes + null logic update for dbm repair |
| `a74b201` | Merge PR #862 - repair persistent queues |
| `e601ce9` | add file command to docker image (fixes #870) |
| `5a7dd87` | document HOST and PORT env vars (closes #815) |
| `e378179` | **BREAKING**: remove playlist strict mode (always true) |
| `9be0781` | **BREAKING**: remove DOWNLOAD_MODE config (always concurrent) |

**New Dependencies**: `gdbm-tools`, `sqlite`, `file` (Alpine packages)

---

## Fork Impact Assessment

### Fork-Specific Features (to preserve)

| Feature | Files | Status |
|---------|-------|--------|
| POT Plugin Support | `pyproject.toml`, `README.md`, `ui/src/app/app.html` | Must preserve |
| Jellyfin NFO Generator | `app/jellyfin_nfo_generator.py` | Additive, no conflict |
| Upstream Sync Workflow | `.github/workflows/upstream-sync-check.yml` | Fork-only, preserve |
| Error Handling | `app/ytdl.py` | **Conflict risk** |
| UI Distinction | `ui/src/app/app.html`, `ui/src/app/app.sass` | **Conflict risk** |

### Conflict Analysis

| File | Fork Changes | Upstream Changes | Conflict Severity |
|------|--------------|------------------|-------------------|
| `app/ytdl.py` | Error handling (7b4106c) | Queue repair, DOWNLOAD_MODE removal, playlist_strict_mode removal | **HIGH** |
| `ui/src/app/app.html` | POT support, UI styling (6b2438c, c04b130) | playlist_strict_mode UI removal | **MEDIUM** |
| `README.md` | POT documentation | HOST/PORT docs added | **LOW** |

### Breaking Changes Impact

1. **`DOWNLOAD_MODE` Removal**
   - Fork currently uses: `app/main.py:73`, `app/ytdl.py:394-425`
   - Action: Remove references, system now always uses concurrent mode with `MAX_CONCURRENT_DOWNLOADS`

2. **`playlist_strict_mode` Removal**
   - Fork currently uses: `app/main.py:247-268`, `app/ytdl.py:463-718`, `ui/src/app/*`
   - Action: Remove all references, playlist strict mode is now always enabled

---

## Rebase Strategy

**Recommended Approach**: Interactive rebase with conflict resolution

### Order of Operations

1. **Create backup branch**
   ```bash
   git checkout master
   git branch backup/master-pre-sync-2026-01-10
   ```

2. **Rebase fork commits onto upstream**
   ```bash
   git checkout master
   git rebase upstream/master
   ```

3. **Resolve conflicts in order**:
   - `app/ytdl.py` - Accept upstream changes for DOWNLOAD_MODE/playlist_strict_mode removal, manually re-apply error handling changes
   - `ui/src/app/app.html` - Accept upstream UI simplification, preserve POT-specific elements
   - `README.md` - Merge both changes (POT docs + HOST/PORT docs)

4. **Verify fork features still work**:
   - POT plugin configuration
   - Jellyfin NFO generation
   - Error handling behavior

5. **Update fork-specific code**:
   - Remove any remaining `DOWNLOAD_MODE` references
   - Remove any remaining `playlist_strict_mode` references
   - Update API calls to match new signatures

### Files Requiring Manual Attention

| File | Action Required |
|------|-----------------|
| `app/ytdl.py` | Re-apply error handling on top of upstream queue repair code |
| `app/main.py` | Remove `DOWNLOAD_MODE` default and `playlist_strict_mode` handling |
| `ui/src/app/app.html` | Merge POT UI elements with upstream simplified UI |
| `ui/src/app/app.ts` | Remove `playlist_strict_mode` from retry logic |
| `ui/src/app/interfaces/download.ts` | Remove `playlist_strict_mode` field |
| `ui/src/app/services/downloads.service.ts` | Remove `playlist_strict_mode` from API calls |
| `Dockerfile` | Ensure new deps (`gdbm-tools`, `sqlite`, `file`) are included |

---

## Testing Checklist

- [ ] Application starts without errors
- [ ] POT plugin loads correctly (check logs for POT initialization)
- [ ] Queue persistence works (add items, restart, verify queue intact)
- [ ] Queue repair mechanism triggers on corrupted DB (test with intentionally corrupted file)
- [ ] NFO files generate correctly for downloaded videos
- [ ] Downloads work in concurrent mode
- [ ] Playlist downloads work (always strict mode now)
- [ ] Error retry functionality works
- [ ] Docker build completes successfully
- [ ] UI displays correctly with POT branding

---

## Rollback Plan

If issues arise after merge:

```bash
# Restore from backup branch
git checkout master
git reset --hard backup/master-pre-sync-2026-01-10
git push --force-with-lease origin master
```

If already deployed:
1. Revert Docker image to previous tag
2. Investigate specific failure
3. Apply targeted fix rather than full rollback if possible

---

## Notes

- Mergeability Score from upstream analysis: **6/10** (moderately invasive)
- Main complexity: Large refactoring in `app/ytdl.py` with fork's error handling changes
- Consider: Breaking this into multiple PRs if conflicts are too complex
