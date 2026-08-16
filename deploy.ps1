# Commit, push, deploy. One command.
#   .\deploy.ps1 "what I changed"

param([Parameter(Mandatory = $true)][string]$Message)

$ErrorActionPreference = "Stop"

git add -A

# `git commit` fails when there is nothing staged, which would abort the whole
# script. A clean tree just means deploy what is already committed.
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m $Message
    git push
} else {
    Write-Host "Nothing to commit; deploying current HEAD."
}

gcloud run deploy humanforai --source . --quiet
