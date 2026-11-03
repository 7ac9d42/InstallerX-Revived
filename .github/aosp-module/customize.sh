#!/system/bin/sh

SKIPUNZIP=0

[ "$API" -ge 26 ] || abort "InstallerX requires Android 8.0 or newer."
[ -f /system/priv-app/PackageInstaller/PackageInstaller.apk ] || abort "Unsupported layout: /system/priv-app/PackageInstaller/PackageInstaller.apk was not found."

ui_print "- Replacing the AOSP PackageInstaller through module mounting"
ui_print "- Requires working systemless mounts and compatible signature checks"
ui_print "- User installation state and HyperOS routing are not changed"
set_perm_recursive "$MODPATH/system" 0 0 0755 0644
set_perm "$MODPATH/action.sh" 0 0 0755
