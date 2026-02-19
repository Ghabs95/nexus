#!/usr/bin/env python3
"""
Setup logrotate configuration for Nexus services.

This script generates the actual logrotate.conf file from the template,
replacing {NEXUS_LOGS_DIR} with the actual logs directory path.

Usage:
    python3 scripts/setup_logrotate.py
    python3 scripts/setup_logrotate.py --install    # Also install to /etc/logrotate.d/
"""

import os
import sys
import argparse
import subprocess
from pathlib import Path


def get_logs_dir():
    """Get logs directory, same way config.py does it."""
    # Same calculation as config.py: os.path.dirname(os.path.dirname(__file__)) -> nexus dir
    nexus_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(nexus_dir, "logs")


def setup_logrotate(install_to_system=False):
    """
    Generate logrotate.conf from template.
    
    Args:
        install_to_system: If True, also install to /etc/logrotate.d/nexus
    """
    nexus_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    template_file = os.path.join(nexus_dir, 'logrotate.conf')
    logs_dir = get_logs_dir()
    
    if not os.path.exists(template_file):
        print(f"❌ Template file not found: {template_file}")
        sys.exit(1)
    
    # Read template
    with open(template_file, 'r') as f:
        content = f.read()
    
    # Replace placeholder with actual logs directory
    actual_config = content.replace('{NEXUS_LOGS_DIR}', logs_dir)
    
    # Write back to same file (or to a temporary location if installing)
    if install_to_system:
        temp_file = '/tmp/nexus-logrotate.conf'
        with open(temp_file, 'w') as f:
            f.write(actual_config)
        
        # Copy to /etc/logrotate.d/ with sudo
        try:
            subprocess.run(
                ['sudo', 'cp', temp_file, '/etc/logrotate.d/nexus'],
                check=True,
                capture_output=True
            )
            os.remove(temp_file)
            print(f"✅ Installed logrotate config to /etc/logrotate.d/nexus")
            print(f"   Logs directory: {logs_dir}")
            
            # Test the config
            result = subprocess.run(
                ['sudo', 'logrotate', '-d', '/etc/logrotate.d/nexus'],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                print(f"✅ Logrotate config validated successfully")
            else:
                print(f"⚠️  Logrotate validation warnings:")
                print(result.stdout)
                if result.stderr:
                    print(result.stderr)
        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to install logrotate config:")
            print(e.stderr.decode() if e.stderr else str(e))
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error: {e}")
            sys.exit(1)
    else:
        # Just update the template file with actual values
        with open(template_file, 'w') as f:
            f.write(actual_config)
        
        print(f"✅ Generated logrotate config")
        print(f"   File: {template_file}")
        print(f"   Logs directory: {logs_dir}")
        print(f"\nTo install to system:")
        print(f"   python3 scripts/setup_logrotate.py --install")
        print(f"\nOr manually:")
        print(f"   sudo cp logrotate.conf /etc/logrotate.d/nexus")


def main():
    parser = argparse.ArgumentParser(
        description='Setup logrotate configuration for Nexus services'
    )
    parser.add_argument(
        '--install',
        action='store_true',
        help='Install to /etc/logrotate.d/nexus (requires sudo)'
    )
    
    args = parser.parse_args()
    setup_logrotate(install_to_system=args.install)


if __name__ == '__main__':
    main()
