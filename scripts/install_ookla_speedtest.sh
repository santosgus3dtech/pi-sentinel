#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Execute como root: sudo scripts/install_ookla_speedtest.sh" >&2
    exit 1
fi

architecture=$(dpkg --print-architecture)
if [ "$architecture" != "arm64" ]; then
    echo "Arquitetura não suportada por este instalador: $architecture (esperado: arm64)." >&2
    exit 1
fi

. /etc/os-release
case "${VERSION_CODENAME:-}" in
    bookworm) distro_version_id=215 ;;
    trixie) distro_version_id=221 ;;
    *)
        echo "Raspberry Pi OS/Debian não reconhecido: ${VERSION_CODENAME:-sem codename}." >&2
        exit 1
        ;;
esac

package_version="1.2.0.84-1.ea6b6773cf"
expected_sha256="98e7de9db3bf181d08bc67e647bcfc71349c8014e387289c08e54e5c55d82f37"
package_file=$(mktemp /tmp/ookla-speedtest.XXXXXX.deb)
trap 'rm -f "$package_file"' EXIT HUP INT TERM
url="https://packagecloud.io/ookla/speedtest-cli/packages/debian/${VERSION_CODENAME}/speedtest_${package_version}_arm64.deb/download.deb?distro_version_id=${distro_version_id}"

curl --fail --location --silent --show-error "$url" --output "$package_file"
actual_sha256=$(sha256sum "$package_file" | cut -d ' ' -f 1)
if [ "$actual_sha256" != "$expected_sha256" ]; then
    echo "Checksum do pacote Ookla não confere." >&2
    exit 1
fi

chmod 0644 "$package_file"
apt-get install -y "$package_file"
install -m 0644 /opt/pi-sentinel/deploy/pi-sentinel-speedtest.service /etc/systemd/system/pi-sentinel-speedtest.service
systemctl daemon-reload
systemctl enable --now pi-sentinel-speedtest.service

/usr/bin/speedtest --version
systemctl --no-pager --full status pi-sentinel-speedtest.service
