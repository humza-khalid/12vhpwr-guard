$TaskName = "12VHPWR Guard"

# Stop first: unregistering alone leaves the running pythonw process alive until logoff.
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}

Write-Host "Uninstalled."
Write-Host "If a tray icon is still visible (manually started instance), right-click it and choose Exit."
