[CmdletBinding()]
param(
 [string[]]$Goals = @('test'),
 [string]$ProjectRoot = (Join-Path $PSScriptRoot '..'),
 [string]$JavaHome = $env:RAG_JAVA_HOME,
 [string]$MavenHome = $env:MAVEN_HOME
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Use Windows native PowerShell.' }
if (-not $JavaHome) { $JavaHome = if (Test-Path -LiteralPath 'D:\JavaSoftWare\jdk21\bin\java.exe') { 'D:\JavaSoftWare\jdk21' } else { $env:JAVA_HOME } }
if (-not $JavaHome) { throw 'Set RAG_JAVA_HOME or supply -JavaHome with JDK 21+.' }
if (-not $MavenHome) { $MavenHome = 'D:\JavaSoftWare\apache-maven-3.8.4-bin\apache-maven-3.8.4' }
$javaExe = Join-Path $JavaHome 'bin/java.exe'
if (-not (Test-Path -LiteralPath $javaExe)) { throw 'Set JAVA_HOME to a JDK 21 directory.' }
$release = Get-Content -LiteralPath (Join-Path $JavaHome 'release') -Raw
if ($release -notmatch 'JAVA_VERSION="(\d+)' -or [int]$Matches[1] -lt 21) { throw 'This project requires JDK 21+. Set RAG_JAVA_HOME; no global Java configuration will be modified.' }
$bootDir = Join-Path $MavenHome 'boot'
$launcher = @(Get-ChildItem -LiteralPath $bootDir -Filter 'plexus-classworlds-*.jar')
if ($launcher.Count -ne 1) { throw 'Set MAVEN_HOME to a Maven distribution.' }
$backend = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot 'backend'))
$settings = Join-Path $PSScriptRoot 'maven-settings.xml'
$repository = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../.runtime/m2'))
& $javaExe "-Dmaven.home=$MavenHome" "-Dmaven.multiModuleProjectDirectory=$backend" "-Dclassworlds.conf=$MavenHome\bin\m2.conf" '-classpath' $launcher[0].FullName 'org.codehaus.plexus.classworlds.launcher.Launcher' '-B' '-ntp' '-s' $settings '-gs' $settings "-Dmaven.repo.local=$repository" '-f' (Join-Path $backend 'pom.xml') @Goals
if ($LASTEXITCODE -ne 0) { throw "Maven failed with exit code $LASTEXITCODE" }
