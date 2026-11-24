-- Run ONCE, only if you already reviewed photos while the LAN API ran in single-share mode
-- (PHOTO_ROOT) and are now switching to CONFIG_FILE (several shares).
--
-- Before: paths looked like   2015/photo.jpg
-- After:  paths look like     Camera Archives/2015/photo.jpg
--
-- Replace both occurrences of the name below with the exact share name you use in the config file.
-- Running this twice would prefix everything twice, so don't.

UPDATE decisions SET
  path    = 'Camera Archives/' || path,
  folder  = CASE WHEN folder = '' THEN 'Camera Archives' ELSE 'Camera Archives/' || folder END,
  trashed = CASE WHEN trashed IS NULL THEN NULL ELSE 'Camera Archives/' || trashed END;

UPDATE meta SET
  value = CASE WHEN value = '' THEN 'Camera Archives' ELSE 'Camera Archives/' || value END
WHERE key = 'last_folder';
