#!/usr/bin/env python3
import os
import json
import random
from datetime import datetime, timedelta

def main():
    summary_file = os.path.join('docs', 'history_summary.json')
    os.makedirs('docs', exist_ok=True)
    
    distros = {
        'archlinux': {
            'pkg_count': 15000,
            'kernel': '7.0.11.arch1-1',
            'gcc': '16.1.1',
            'python3': '3.14.5-1',
            'glibc': '2.43',
            'systemd': '260.2-2',
            'gnome-shell': '50.2-1'
        },
        'opensuse-tumbleweed': {
            'pkg_count': 40000,
            'kernel': '7.0.10-default',
            'gcc': '16.1.0',
            'python3': '3.14.4',
            'glibc': '2.43',
            'systemd': '260.1-1',
            'gnome-shell': '50.1-1'
        },
        'opensuse-slowroll': {
            'pkg_count': 38000,
            'kernel': '7.0.5-default',
            'gcc': '15.2.0',
            'python3': '3.12.3',
            'glibc': '2.41',
            'systemd': '256.3-1',
            'gnome-shell': '48.2-1'
        },
        'opensuse-leap': {
            'pkg_count': 36000,
            'kernel': '6.12.18-default',
            'gcc': '14.2.0',
            'python3': '3.11.9',
            'glibc': '2.39',
            'systemd': '254.12-1',
            'gnome-shell': '46.4-1'
        },
        'fedora': {
            'pkg_count': 68000,
            'kernel': '7.0.9-200.fc44',
            'gcc': '16.0.1',
            'python3': '3.14.2',
            'glibc': '2.43',
            'systemd': '260.0-3',
            'gnome-shell': '50.0-2'
        },
        'ubuntu': {
            'pkg_count': 75000,
            'kernel': '6.15.0-30-generic',
            'gcc': '15.1.0',
            'python3': '3.12.3',
            'glibc': '2.41',
            'systemd': '256.4-2ubuntu3',
            'gnome-shell': '48.0-1ubuntu1'
        }
    }
    
    data = {
        'history': {},
        'versions': {},
        'latest_changelog': {}
    }
    
    today = datetime.now().date()
    
    # Generate 14 days of history
    for i in range(14, -1, -1):
        date_str = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        data['history'][date_str] = {}
        data['versions'][date_str] = {}
        
        for distro, info in distros.items():
            # Churn increases randomly
            pkg_growth = random.randint(-5, 15)
            info['pkg_count'] += pkg_growth
            
            # Updates count
            if distro in ('archlinux', 'opensuse-tumbleweed'):
                updated = random.randint(10, 150)
                new_pkgs = random.randint(1, 10)
                removed = random.randint(0, 5)
            elif distro == 'opensuse-slowroll':
                # Slowroll updates in batches
                if i % 7 == 0:
                    updated = random.randint(100, 400)
                    new_pkgs = random.randint(5, 25)
                    removed = random.randint(1, 10)
                else:
                    updated = 0
                    new_pkgs = 0
                    removed = 0
            else: # Leap, Fedora, Ubuntu are more stable
                updated = random.randint(0, 20)
                new_pkgs = random.randint(0, 2)
                removed = random.randint(0, 1)
                
            size_bytes = updated * random.randint(100000, 5000000) if updated > 0 else 0
            
            data['history'][date_str][distro] = {
                'package_count': info['pkg_count'],
                'updated': updated,
                'new': new_pkgs,
                'removed': removed,
                'size_bytes': size_bytes
            }
            
            # Versions slightly evolve
            data['versions'][date_str][distro] = {
                'kernel': info['kernel'],
                'systemd': info['systemd'],
                'python3': info['python3'],
                'gcc': info['gcc'],
                'glibc': info['glibc'],
                'gnome-shell': info['gnome-shell'],
                'wayland': '1.25.0',
                'bash': '5.3'
            }
            
    # Generate mock latest changelog
    for distro in distros.keys():
        data['latest_changelog'][distro] = {
            'updated': [
                ['linux-kernel-mock', '6.14.0', '7.0.0', 12500000],
                ['python-mock-pkg', '3.12.0', '3.14.0', 4500000],
                ['gcc-compiler-mock', '15.0.0', '16.0.0', 25000000],
                ['glibc-libs-mock', '2.40', '2.43', 8000000]
            ],
            'new': [
                ['brand-new-package', '1.0.0', 500000],
                ['another-cool-tool', '0.1.2', 1200000]
            ],
            'removed': [
                ['deprecated-utility', '2.4-5']
            ]
        }
        
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
        
    print(f"Generated mock history with 15 dates: {summary_file}")

if __name__ == '__main__':
    main()
