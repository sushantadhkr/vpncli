$ErrorActionPreference = "Stop"

$Repo = if ($env:VPNCLI_REPO) { $env:VPNCLI_REPO } else { "https://github.com/sushantadhkr/vpncli.git" }
$Spec = if ($env:VPNCLI_NO_GUI -eq "1") { "vpncli @ git+$Repo" } else { "vpncli[gui] @ git+$Repo" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git is required" }

if (Get-Command pipx -ErrorAction SilentlyContinue) {
    pipx install --force $Spec
} else {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "Python 3.8 or newer is required" }
    python -m pip install --user --upgrade pipx
    python -m pipx ensurepath
    python -m pipx install --force $Spec
}

if (-not (Get-Command openvpn -ErrorAction SilentlyContinue)) {
    Write-Host "OpenVPN was not found. Install it with: winget install OpenVPNTechnologies.OpenVPN"
}
Write-Host "vpncli installed. Open a new terminal and run: vpncli --help"
Write-Host "To add the app icon and tray entry, run: vpncli register"
Write-Host "To remove it later: vpncli uninstall"
