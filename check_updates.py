#!/usr/bin/env python3
import os
import sys
import json
import gzip
import tarfile
import lzma
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
import urllib.error
import time
import tempfile
import subprocess
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configuration
HISTORY_DIR = 'history'
DOCS_DIR = 'docs'
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# Helper to download with retries and realistic User-Agent
def download_file(url, output_path, retries=3, delay=2):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=45) as response, open(output_path, 'wb') as out_file:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    out_file.write(chunk)
            return True
        except urllib.error.URLError as e:
            print(f"Error downloading {url} (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise e

# Parse Fedora Metalink to get a working repomd.xml URL
def resolve_metalink_to_repomd(metalink_url):
    print(f"Resolving metalink: {metalink_url}")
    with tempfile.NamedTemporaryFile(suffix='.xml', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        download_file(metalink_url, tmp_path)
        tree = ET.parse(tmp_path)
        root = tree.getroot()
        urls = root.findall('.//{http://www.metalinker.org/}url')
        if not urls:
            urls = root.findall('.//{urn:ietf:params:xml:ns:metalink}url')
        if not urls:
            urls = root.findall('.//url')
            
        for url in urls:
            protocol = url.attrib.get('protocol')
            if protocol in ('https', 'http') or url.text.startswith('http'):
                repomd_url = url.text.strip()
                print(f"Resolved to repomd: {repomd_url}")
                return repomd_url
        raise ValueError(f"No http/https url found in metalink: {metalink_url}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

# Parse repomd.xml to get primary.xml.gz/xz/zst href
def parse_repomd_xml(repomd_path):
    tree = ET.parse(repomd_path)
    root = tree.getroot()
    ns = {'ns': 'http://linux.duke.edu/metadata/repo'}
    data_elems = root.findall(".//ns:data[@type='primary']", ns)
    if not data_elems:
        data_elems = root.findall(".//data[@type='primary']")
        
    if data_elems:
        loc = data_elems[0].find('ns:location', ns)
        if loc is None:
            loc = data_elems[0].find('location')
        if loc is not None:
            return loc.attrib.get('href')
    raise ValueError("Primary repository metadata location not found in repomd.xml")

# Parse repomd primary metadata XML (stream-based)
def parse_repomd_primary(file_path):
    temp_decompressed = None
    f = None
    try:
        if file_path.endswith('.gz'):
            f = gzip.open(file_path, 'rb')
        elif file_path.endswith('.xz'):
            f = lzma.open(file_path, 'rb')
        elif file_path.endswith('.zst') or file_path.endswith('.zstd'):
            temp_decompressed = file_path + '.decompressed'
            try:
                subprocess.run(['zstd', '-d', '-f', '-o', temp_decompressed, file_path], check=True, capture_output=True)
                f = open(temp_decompressed, 'rb')
            except FileNotFoundError:
                raise RuntimeError("zstd CLI command not found. Please install zstd.")
        else:
            f = open(file_path, 'rb')
            
        packages = {}
        context = ET.iterparse(f, events=('start', 'end'))
        current_pkg = None
        
        for event, elem in context:
            is_pkg_tag = elem.tag.endswith('package')
            
            if event == 'start' and is_pkg_tag:
                current_pkg = {'name': '', 'version': '', 'size': 0}
            elif event == 'end' and is_pkg_tag:
                if current_pkg and current_pkg['name']:
                    packages[current_pkg['name']] = (current_pkg['version'], current_pkg['size'])
                elem.clear()
            elif event == 'end' and current_pkg is not None:
                tag_name = elem.tag.split('}')[-1]
                if tag_name == 'name':
                    current_pkg['name'] = elem.text if elem.text else ''
                elif tag_name == 'version':
                    epoch = elem.attrib.get('epoch', '0')
                    ver = elem.attrib.get('ver', '')
                    rel = elem.attrib.get('rel', '')
                    current_pkg['version'] = f"{epoch}:{ver}-{rel}" if epoch != '0' else f"{ver}-{rel}"
                elif tag_name == 'size':
                    current_pkg['size'] = int(elem.attrib.get('package', '0') or '0')
    finally:
        if f is not None:
            f.close()
        if temp_decompressed and os.path.exists(temp_decompressed):
            os.remove(temp_decompressed)
            
    return packages

# Parse Arch Linux package database (.db file)
def parse_arch_db(file_path):
    packages = {}
    with tarfile.open(file_path, "r:gz") as tar:
        for member in tar:
            if member.name.endswith('/desc'):
                f = tar.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode('utf-8', errors='replace')
                
                lines = content.strip().split('\n')
                name = None
                version = None
                csize = 0
                
                current_field = None
                field_lines = []
                
                def process_field(field, val_lines):
                    nonlocal name, version, csize
                    if field == '%NAME%':
                        name = ''.join(val_lines).strip()
                    elif field == '%VERSION%':
                        version = ''.join(val_lines).strip()
                    elif field == '%CSIZE%':
                        csize = int(''.join(val_lines).strip() or '0')

                for line in lines:
                    line = line.strip()
                    if line.startswith('%') and line.endswith('%'):
                        if current_field:
                            process_field(current_field, field_lines)
                        current_field = line
                        field_lines = []
                    else:
                        if line:
                            field_lines.append(line)
                if current_field:
                    process_field(current_field, field_lines)
                    
                if name:
                    packages[name] = (version, csize)
    return packages

# Parse Debian/Ubuntu Packages.gz (stream-based)
def parse_debian_packages(file_path):
    packages = {}
    with gzip.open(file_path, 'rt', encoding='utf-8', errors='replace') as f:
        current_pkg = {}
        for line in f:
            if line.startswith(' ') or line.startswith('\t'):
                continue
            line = line.strip()
            if not line:
                if 'Package' in current_pkg:
                    name = current_pkg['Package']
                    version = current_pkg.get('Version', '')
                    size = int(current_pkg.get('Size', '0') or '0')
                    packages[name] = (version, size)
                current_pkg = {}
                continue
            if ':' in line:
                key, val = line.split(':', 1)
                current_pkg[key.strip()] = val.strip()
        if 'Package' in current_pkg:
            packages[current_pkg['Package']] = (current_pkg.get('Version', ''), int(current_pkg.get('Size', '0') or '0'))
    return packages

# High-level crawler function for each distro
def crawl_distro(distro, config):
    print(f"\n--- Crawling {distro} ---")
    merged_packages = {}
    
    if config['type'] == 'arch':
        for url in config['urls']:
            print(f"Downloading Arch DB: {url}")
            with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
                tmp_path = tmp.name
            try:
                download_file(url, tmp_path)
                packages = parse_arch_db(tmp_path)
                print(f"Parsed {len(packages)} packages.")
                merged_packages.update(packages)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                    
    elif config['type'] == 'repomd':
        for repo_info in config['repos']:
            repomd_xml_url = repo_info.get('repomd')
            base_url = repo_info.get('base')
            
            if repo_info.get('metalink'):
                try:
                    repomd_xml_url = resolve_metalink_to_repomd(repo_info['metalink'])
                    base_url = repomd_xml_url.rsplit('/repodata/', 1)[0] + '/'
                except Exception as e:
                    print(f"Failed to resolve metalink {repo_info['metalink']}: {e}")
                    continue
            
            print(f"Downloading repomd.xml: {repomd_xml_url}")
            with tempfile.NamedTemporaryFile(suffix='.xml', delete=False) as tmp_repomd:
                tmp_repomd_path = tmp_repomd.name
            try:
                download_file(repomd_xml_url, tmp_repomd_path)
                primary_href = parse_repomd_xml(tmp_repomd_path)
                primary_url = urllib.parse.urljoin(base_url, primary_href)
                
                print(f"Downloading primary metadata: {primary_url}")
                if primary_href.endswith('.gz'):
                    suffix = '.xml.gz'
                elif primary_href.endswith('.xz'):
                    suffix = '.xml.xz'
                elif primary_href.endswith('.zst') or primary_href.endswith('.zstd'):
                    suffix = '.xml.zst'
                else:
                    suffix = '.xml'
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_primary:
                    tmp_primary_path = tmp_primary.name
                try:
                    download_file(primary_url, tmp_primary_path)
                    packages = parse_repomd_primary(tmp_primary_path)
                    print(f"Parsed {len(packages)} packages.")
                    merged_packages.update(packages)
                except Exception as e:
                    print(f"Error parsing primary metadata {primary_url}: {e}")
                    raise e
                finally:
                    if os.path.exists(tmp_primary_path):
                        os.remove(tmp_primary_path)
            finally:
                if os.path.exists(tmp_repomd_path):
                    os.remove(tmp_repomd_path)
                    
    elif config['type'] == 'debian':
        base_url = config['base']
        for dist in config['dists']:
            for comp in config['components']:
                url = urllib.parse.urljoin(base_url, f"dists/{dist}/{comp}/binary-amd64/Packages.gz")
                print(f"Downloading Debian Packages: {url}")
                with tempfile.NamedTemporaryFile(suffix='.gz', delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    download_file(url, tmp_path)
                    packages = parse_debian_packages(tmp_path)
                    print(f"Parsed {len(packages)} packages.")
                    merged_packages.update(packages)
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        print(f"Not found: {url} (skipping)")
                    else:
                        print(f"HTTP Error {e.code} for {url}")
                except Exception as e:
                    print(f"Error crawling {url}: {e}")
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                        
    return merged_packages

# Load snapshot from history
def load_snapshot(file_path):
    with gzip.open(file_path, 'rt', encoding='utf-8') as f:
        return json.load(f)

# Save snapshot to history
def save_snapshot(file_path, packages):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with gzip.open(file_path, 'wt', encoding='utf-8') as f:
        json.dump(packages, f)

# Find closest snapshot in history
def find_snapshot(distro, target_date, max_offset_days=3, exclude_today=True):
    import re
    best_file = None
    best_diff = timedelta(days=max_offset_days + 1)
    pattern = re.compile(rf"^{re.escape(distro)}_(\d{{4}}-\d{{2}}-\d{{2}})\.json\.gz$")
    
    if not os.path.exists(HISTORY_DIR):
        return None, None
        
    today = datetime.now().date()
    for fname in os.listdir(HISTORY_DIR):
        m = pattern.match(fname)
        if m:
            file_date_str = m.group(1)
            try:
                file_date = datetime.strptime(file_date_str, '%Y-%m-%d').date()
            except ValueError:
                continue
            if exclude_today and file_date == today:
                continue
            diff = abs(file_date - target_date)
            if diff <= timedelta(days=max_offset_days) and diff < best_diff:
                best_diff = diff
                best_file = os.path.join(HISTORY_DIR, fname)
                
    if best_file:
        date_str = os.path.basename(best_file).split('_')[1].split('.')[0]
        return best_file, datetime.strptime(date_str, '%Y-%m-%d').date()
    return None, None

# Compare two package snapshots and return detailed difference lists
def compare_snapshots_detailed(current, previous):
    updated = []
    new_pkgs = []
    removed = []
    total_update_size = 0
    
    for pkg, (ver, size) in current.items():
        if pkg not in previous:
            new_pkgs.append({'name': pkg, 'version': ver, 'size': size})
            total_update_size += size
        else:
            prev_ver, _ = previous[pkg]
            if ver != prev_ver:
                updated.append({'name': pkg, 'old_version': prev_ver, 'new_version': ver, 'size': size})
                total_update_size += size
                
    for pkg, (ver, size) in previous.items():
        if pkg not in current:
            removed.append({'name': pkg, 'version': ver})
            
    updated.sort(key=lambda x: x['name'])
    new_pkgs.sort(key=lambda x: x['name'])
    removed.sort(key=lambda x: x['name'])
    
    return {
        'updated': updated,
        'new': new_pkgs,
        'removed': removed,
        'size_bytes': total_update_size
    }

# Extract versions of key software
def extract_key_versions(packages, distro):
    mapping = {
        'kernel': {
            'archlinux': 'linux',
            'opensuse-tumbleweed': 'kernel-default',
            'opensuse-slowroll': 'kernel-default',
            'opensuse-leap': 'kernel-default',
            'fedora': 'kernel',
            'ubuntu': 'linux-generic'
        },
        'systemd': {
            'archlinux': 'systemd',
            'opensuse-tumbleweed': 'systemd',
            'opensuse-slowroll': 'systemd',
            'opensuse-leap': 'systemd',
            'fedora': 'systemd',
            'ubuntu': 'systemd'
        },
        'python3': {
            'archlinux': 'python',
            'opensuse-tumbleweed': ['python315', 'python314', 'python313', 'python312', 'python311', 'python3'],
            'opensuse-slowroll': ['python315', 'python314', 'python313', 'python312', 'python311', 'python3'],
            'opensuse-leap': ['python315', 'python314', 'python313', 'python312', 'python311', 'python3'],
            'fedora': 'python3',
            'ubuntu': 'python3'
        },
        'gcc': {
            'archlinux': 'gcc',
            'opensuse-tumbleweed': 'gcc',
            'opensuse-slowroll': 'gcc',
            'opensuse-leap': 'gcc',
            'fedora': 'gcc',
            'ubuntu': 'gcc'
        },
        'glibc': {
            'archlinux': 'glibc',
            'opensuse-tumbleweed': 'glibc',
            'opensuse-slowroll': 'glibc',
            'opensuse-leap': 'glibc',
            'fedora': 'glibc',
            'ubuntu': 'libc6'
        },
        'gnome-shell': {
            'archlinux': 'gnome-shell',
            'opensuse-tumbleweed': 'gnome-shell',
            'opensuse-slowroll': 'gnome-shell',
            'opensuse-leap': 'gnome-shell',
            'fedora': 'gnome-shell',
            'ubuntu': 'gnome-shell'
        },
        'wayland': {
            'archlinux': 'wayland',
            'opensuse-tumbleweed': 'wayland',
            'opensuse-slowroll': 'wayland',
            'opensuse-leap': 'wayland',
            'fedora': 'wayland',
            'ubuntu': 'wayland'
        },
        'bash': {
            'archlinux': 'bash',
            'opensuse-tumbleweed': 'bash',
            'opensuse-slowroll': 'bash',
            'opensuse-leap': 'bash',
            'fedora': 'bash',
            'ubuntu': 'bash'
        }
    }
    
    versions = {}
    for sw, dist_map in mapping.items():
        pkg_names = dist_map.get(distro)
        if isinstance(pkg_names, str):
            pkg_names = [pkg_names]
            
        version_found = 'N/A'
        for pkg_name in pkg_names:
            if pkg_name in packages:
                version_found = packages[pkg_name][0]
                break
        versions[sw] = version_found
    return versions

# Format byte sizes human-readably
def format_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024
        i += 1
    return f"{size_bytes:.2f} {units[i]}"

# Clean up snapshots older than 35 days
def cleanup_history():
    if not os.path.exists(HISTORY_DIR):
        return
    now = datetime.now()
    retention_limit = now - timedelta(days=35)
    
    deleted_count = 0
    for fname in os.listdir(HISTORY_DIR):
        if not fname.endswith('.json.gz'):
            continue
        parts = fname.rsplit('_', 1)
        if len(parts) == 2:
            date_str = parts[1].split('.')[0]
            try:
                file_date = datetime.strptime(date_str, '%Y-%m-%d')
                if file_date < retention_limit:
                    os.remove(os.path.join(HISTORY_DIR, fname))
                    deleted_count += 1
            except ValueError:
                pass
    if deleted_count > 0:
        print(f"Cleaned up {deleted_count} expired snapshot files older than 35 days.")

# Write consolidated JSON summary for the web dashboard
def update_history_summary(today_str, current_stats, current_versions, detailed_changelogs):
    summary_file = os.path.join(DOCS_DIR, 'history_summary.json')
    os.makedirs(DOCS_DIR, exist_ok=True)
    
    if os.path.exists(summary_file):
        try:
            with open(summary_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            print(f"Error reading {summary_file}, resetting: {e}")
            data = {}
    else:
        data = {}
        
    if 'history' not in data:
        data['history'] = {}
    if 'versions' not in data:
        data['versions'] = {}
    if 'latest_changelog' not in data:
        data['latest_changelog'] = {}
        
    # Add today's statistics and versions
    data['history'][today_str] = current_stats
    data['versions'][today_str] = current_versions
    
    # Store detailed changelog for the latest run
    for distro, changelog in detailed_changelogs.items():
        data['latest_changelog'][distro] = changelog
        
    # Prune historical chart stats older than 60 days
    all_dates = sorted(list(data['history'].keys()))
    if len(all_dates) > 60:
        dates_to_remove = all_dates[:-60]
        for d in dates_to_remove:
            if d in data['history']:
                del data['history'][d]
            if d in data['versions']:
                del data['versions'][d]
                
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    print(f"Successfully updated consolidated summary: {summary_file}")

# Send Slack or Discord notification
def send_webhooks(report_content):
    discord_url = os.environ.get('DISCORD_WEBHOOK_URL')
    slack_url = os.environ.get('SLACK_WEBHOOK_URL')
    
    if not discord_url and not slack_url:
        return
        
    print("Sending webhook alerts...")
    
    if discord_url:
        compact_content = report_content
        # Discord limit is 2000 characters
        if len(compact_content) > 1900:
            compact_content = compact_content[:1900] + "\n... (truncated, check GitHub Actions for full report)"
        payload = {"content": compact_content}
        try:
            req = urllib.request.Request(
                discord_url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT}
            )
            with urllib.request.urlopen(req) as r:
                r.read()
            print("Successfully sent Discord webhook.")
        except Exception as e:
            print(f"Failed to send Discord webhook: {e}")
            
    if slack_url:
        payload = {"text": report_content}
        try:
            req = urllib.request.Request(
                slack_url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT}
            )
            with urllib.request.urlopen(req) as r:
                r.read()
            print("Successfully sent Slack webhook.")
        except Exception as e:
            print(f"Failed to send Slack webhook: {e}")

def main():
    today = datetime.now().date()
    today_str = today.strftime('%Y-%m-%d')
    
    distros = {
        'archlinux': {
            'type': 'arch',
            'urls': [
                'https://mirrors.kernel.org/archlinux/core/os/x86_64/core.db',
                'https://mirrors.kernel.org/archlinux/extra/os/x86_64/extra.db'
            ]
        },
        'opensuse-tumbleweed': {
            'type': 'repomd',
            'repos': [
                {
                    'repomd': 'https://download.opensuse.org/tumbleweed/repo/oss/repodata/repomd.xml',
                    'base': 'https://download.opensuse.org/tumbleweed/repo/oss/'
                }
            ]
        },
        'opensuse-slowroll': {
            'type': 'repomd',
            'repos': [
                {
                    'repomd': 'https://download.opensuse.org/slowroll/repo/oss/repodata/repomd.xml',
                    'base': 'https://download.opensuse.org/slowroll/repo/oss/'
                }
            ]
        },
        'opensuse-leap': {
            'type': 'repomd',
            'repos': [
                {
                    'repomd': 'https://download.opensuse.org/distribution/leap/16.0/repo/oss/repodata/repomd.xml',
                    'base': 'https://download.opensuse.org/distribution/leap/16.0/repo/oss/'
                }
            ]
        },
        'fedora': {
            'type': 'repomd',
            'repos': [
                {
                    'metalink': 'https://mirrors.fedoraproject.org/metalink?repo=fedora-44&arch=x86_64'
                },
                {
                    'metalink': 'https://mirrors.fedoraproject.org/metalink?repo=updates-released-f44&arch=x86_64'
                }
            ]
        },
        'ubuntu': {
            'type': 'debian',
            'base': 'http://archive.ubuntu.com/ubuntu/',
            'dists': ['resolute', 'resolute-updates', 'resolute-security'],
            'components': ['main', 'restricted', 'universe', 'multiverse']
        }
    }
    
    # Filter by command-line arguments if specified
    if len(sys.argv) > 1:
        requested = sys.argv[1:]
        distros = {k: v for k, v in distros.items() if k in requested}
        if not distros:
            print(f"None of the requested distros {requested} are configured.")
            sys.exit(1)
            
    results = {}
    errors = []
    
    # Track dashboard summary metrics
    detailed_changelogs = {}
    current_stats = {}
    current_versions = {}
    
    def process_single_distro(item):
        distro, config = item
        try:
            current_pkgs = crawl_distro(distro, config)
            if not current_pkgs:
                raise ValueError("No packages retrieved.")
                
            print(f"Total package count for {distro}: {len(current_pkgs)}")
            
            # Save today's snapshot
            today_file = os.path.join(HISTORY_DIR, f"{distro}_{today_str}.json.gz")
            save_snapshot(today_file, current_pkgs)
            
            # Extract key software versions
            versions = extract_key_versions(current_pkgs, distro)
            
            # Calculate T-1 stats and changelog for dashboard summary
            yesterday_target = today - timedelta(days=1)
            snap_path, actual_date = find_snapshot(distro, yesterday_target)
            if snap_path:
                try:
                    prev_pkgs = load_snapshot(snap_path)
                    detailed = compare_snapshots_detailed(current_pkgs, prev_pkgs)
                    detailed_changelog = {
                        'updated': [[pkg['name'], pkg['old_version'], pkg['new_version'], pkg['size']] for pkg in detailed['updated']],
                        'new': [[pkg['name'], pkg['version'], pkg['size']] for pkg in detailed['new']],
                        'removed': [[pkg['name'], pkg['version']] for pkg in detailed['removed']]
                    }
                    stats = {
                        'package_count': len(current_pkgs),
                        'updated': len(detailed['updated']),
                        'new': len(detailed['new']),
                        'removed': len(detailed['removed']),
                        'size_bytes': detailed['size_bytes']
                    }
                except Exception as ex:
                    print(f"Error loading snapshot for summary {snap_path}: {ex}")
                    stats = {'package_count': len(current_pkgs), 'updated': 0, 'new': 0, 'removed': 0, 'size_bytes': 0}
                    detailed_changelog = {'updated': [], 'new': [], 'removed': []}
            else:
                stats = {'package_count': len(current_pkgs), 'updated': 0, 'new': 0, 'removed': 0, 'size_bytes': 0}
                detailed_changelog = {'updated': [], 'new': [], 'removed': []}
            
            # Look up comparisons for Markdown report periods
            periods = {
                '1 Day Ago': today - timedelta(days=1),
                '1 Week Ago': today - timedelta(days=7),
                '1 Month Ago': today - timedelta(days=30)
            }
            
            distro_results = {'package_count': len(current_pkgs)}
            for period_name, target_date in periods.items():
                snap_path, actual_date = find_snapshot(distro, target_date)
                if snap_path:
                    try:
                        prev_pkgs = load_snapshot(snap_path)
                        detailed = compare_snapshots_detailed(current_pkgs, prev_pkgs)
                        distro_results[period_name] = {
                            'actual_date': actual_date.strftime('%Y-%m-%d'),
                            'updated': len(detailed['updated']),
                            'new': len(detailed['new']),
                            'removed': len(detailed['removed']),
                            'size_bytes': detailed['size_bytes']
                        }
                    except Exception as ex:
                        print(f"Error loading snapshot {snap_path}: {ex}")
                        distro_results[period_name] = None
                else:
                    distro_results[period_name] = None
            
            return {
                'distro': distro,
                'stats': stats,
                'versions': versions,
                'detailed_changelog': detailed_changelog,
                'distro_results': distro_results,
                'error': None
            }
        except Exception as e:
            return {
                'distro': distro,
                'stats': None,
                'versions': None,
                'detailed_changelog': None,
                'distro_results': None,
                'error': str(e)
            }
            
    max_workers = min(len(distros), 6)
    print(f"Starting parallel crawl with ThreadPoolExecutor (max_workers={max_workers})...")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_single_distro, item) for item in distros.items()]
        for future in as_completed(futures):
            res = future.result()
            distro = res['distro']
            if res['error'] is not None:
                print(f"Failed to process {distro}: {res['error']}")
                errors.append((distro, res['error']))
            else:
                current_stats[distro] = res['stats']
                current_versions[distro] = res['versions']
                detailed_changelogs[distro] = res['detailed_changelog']
                results[distro] = res['distro_results']
            
    # Clean up old snapshots
    cleanup_history()
    
    # Save the consolidated JSON history summary if we retrieved data
    if current_stats:
        update_history_summary(today_str, current_stats, current_versions, detailed_changelogs)
        
    # Generate Markdown Report
    report = []
    report.append(f"# Linux Package Update Report - {today_str}\n")
    
    if errors:
        report.append("### ⚠️ Errors Encountered During Crawl")
        for distro, err in errors:
            report.append(f"- **{distro}**: {err}")
        report.append("\n")
        
    for distro, data in results.items():
        report.append(f"## 📦 {distro.upper()} (Total Packages: {data['package_count']:,})")
        
        # Display key software versions
        if distro in current_versions:
            vers = current_versions[distro]
            report.append(f"**Core Software**: Kernel: `{vers['kernel']}` | GCC: `{vers['gcc']}` | Python3: `{vers['python3']}` | Systemd: `{vers['systemd']}`")
            report.append("")
            
        report.append("| Period | Reference Date | Updated Pkgs | New Pkgs | Removed Pkgs | Est. Download Size |")
        report.append("|---|---|---|---|---|---|")
        
        has_comparison = False
        for period in ['1 Day Ago', '1 Week Ago', '1 Month Ago']:
            comp = data[period]
            if comp:
                has_comparison = True
                size_str = format_size(comp['size_bytes'])
                report.append(f"| {period} | {comp['actual_date']} | {comp['updated']:,} | {comp['new']:,} | {comp['removed']:,} | {size_str} |")
            else:
                report.append(f"| {period} | N/A | - | - | - | - |")
                
        if not has_comparison:
            report.append("\n*No historical snapshots available for comparison yet.*\n")
        else:
            report.append("\n")
            
    report_content = '\n'.join(report)
    print("\n" + "="*40 + "\nFINAL SUMMARY\n" + "="*40)
    print(report_content)
    
    # Write to GITHUB_STEP_SUMMARY if running in GitHub Actions
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        with open(summary_path, 'a', encoding='utf-8') as f:
            f.write(report_content)
            
    # Send webhooks
    send_webhooks(report_content)
            
if __name__ == '__main__':
    main()
