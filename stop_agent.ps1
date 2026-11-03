# Stops every running copy of the review agent (python -m review_agent) on Windows.
# Its progress is saved after each merge request, so stopping mid-round loses at most the review in progress.
$agents = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match '-m\s+review_agent' }
if (-not $agents) {
    "No review agent is running."
    exit 0
}
foreach ($agent in $agents) {
    Stop-Process -Id $agent.ProcessId -Force
    "Stopped review agent (PID $($agent.ProcessId))."
}
