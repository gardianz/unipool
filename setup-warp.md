## 2. WARP + Tailscale (anti-429) — setup lengkap

**Kenapa:** OTP Privy dibatasi per source-IP. IP VPS polos cepat kena 429. WARP exit via IP
Cloudflare bersih → OTP lolos. Dua mode WARP:

| Mode | Perintah | Exit IP | SSH | OTP |
|---|---|---|---|---|
| **proxy** | `warp-cli mode proxy` | fix per-VPS (bisa kebakar volume, reda sendiri ~1 hari) | selalu aman | lewat SOCKS `:40000` |
| **full-tunnel** | `warp-cli mode warp` | IP Cloudflare paling bersih (**disarankan**) | aman **hanya** via Tailscale | native |

**Rekomendasi:** full-tunnel + Tailscale (paling bersih, kebal 429). Kalau mau simpel & tak mau
ribet Tailscale → proxy mode cukup (SSH selalu aman, IP reda sendiri).

### 2a. Kenapa full-tunnel bisa putus SSH
`mode warp` reroute **SEMUA** traffic VPS (semua user & proses) lewat WireGuard WARP, **termasuk
jalur balik SSH**. Kalau SSH-mu lewat IP publik yg tak di-exclude → paket balik masuk tunnel →
**koneksi drop**. IP rumah/HP biasanya **dinamis** (ganti tiap mati listrik/router restart) →
exclude cepat basi → lock. **Solusi permanen = SSH lewat Tailscale (IP tetap).**

### 2b. Tailscale (SSH IP tetap, kebal ganti IP)
```bash
# di VPS
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up                              # buka URL login → approve device di browser
sudo tailscale ip -4                           # catat IP VPS Tailscale, mis. 100.x.y.z
warp-cli tunnel ip add-range 100.64.0.0/10     # exclude range Tailscale dari WARP (persist)
```
- Install Tailscale juga di **komputer klien** ([tailscale.com/download](https://tailscale.com/download)),
  login akun SAMA. SSH client apapun (terminal, MobaXterm, PuTTY) konek ke IP Tailscale VPS:
  `ssh hermes@100.x.y.z`.
- Di admin console ([login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines)):
  klik mesin VPS → **Disable key expiry** biar tak lepas dari tailnet tiap ~6 bulan.
- Range `100.64.0.0/10` ter-exclude → SSH Tailscale **tak pernah** masuk tunnel WARP → aman
  walau IP publik ISP berubah.

### 2c. Flip ke full-tunnel (aman)
Setelah Tailscale jalan & SSH via IP Tailscale terverifikasi:
```bash
bash scripts/warp-full.sh          # auto-detect IP SSH aktif + exclude + arm auto-revert 90s + flip
```
`.env`: `WARP_PROXY=` kosong + `WARP_FULLTUNNEL=1`. Helper `warp-full.sh` arm auto-revert (kalau
SSH toh mati → balik proxy mode sendiri) jadi tak bisa lock permanen.

### 2d. Tailscale BYPASS WARP full-tunnel (WAJIB kalau full-tunnel) — fix putus/hang
Exclude `100.64.0.0/10` (Section 2b) cuma melindungi **overlay** Tailscale (IP `100.x`). Tapi
**underlay** Tailscale (paket WireGuard `tailscaled` ke server DERP / peer di IP publik) tetap
ke-tangkap WARP full-tunnel. Sebabnya rule WARP `5209: not from all fwmark 0x100cf lookup 65743`
menangkap SEMUA paket yg mark-nya bukan `0x100cf` (mark WARP sendiri) → termasuk mark bypass
Tailscale `0x80000`. Akibat: Tailscale jadi **nested di dalam WARP** (dobel WireGuard/MASQUE):

- MTU jebol → burst besar (screen redraw, buka Claude Code) **hang/lag**, dan
- kalau WARP macet/mati → Tailscale ikut mati **TOTAL** (putus tak balik) → terpaksa reboot VPS.

Fix: sisipkan ip rule prioritas `5208` (< 5209) yg loloskan mark Tailscale ke tabel `main`
(eth0 native) SEBELUM WARP menangkap. Tailscale jadi **independen dari WARP** — WARP mati pun SSH
tetap hidup. Bot OTP tetap full-tunnel (anti-429).
```bash
sudo cp systemd/ts-warp-bypass.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ts-warp-bypass.service
ip rule show | grep 5208                     # harus muncul SEBELUM baris 5209
ip route get 1.1.1.1 mark 0x80000            # harus 'dev eth0' (BUKAN CloudflareWARP)
```
Opsional (mitigasi tambahan) — kecilkan MTU Tailscale biar tetap muat walau rule sempat hilang:
buat `/etc/systemd/system/tailscaled.service.d/mtu.conf` isi `[Service]` + baris
`Environment=TS_DEBUG_MTU=1180`, lalu `sudo systemctl daemon-reload && sudo systemctl restart tailscaled`.

### 2e. Auto-start saat boot (systemd)
```bash
sudo cp systemd/warp-connect.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now warp-connect.service
warp-cli status                                    # harus Connected
```
Unit (versi ini) boot ke **full-tunnel** + re-assert exclude Tailscale tiap boot (idempoten) →
aman karena SSH via Tailscale. **Kalau BELUM pakai Tailscale**, ganti `ExecStart` ke varian
proxy mode (lihat komentar di file `systemd/warp-connect.service`). Pasang juga
`ts-warp-bypass.service` (Section 2d) — WAJIB bareng full-tunnel.

### 2f. Kalau ke-lock SSH (darurat)
Masuk lewat **console panel VPS** (web, bukan SSH), jalankan:
```bash
warp-cli mode proxy && warp-cli connect     # native lagi, SSH publik pulih
```

### 2g. Verifikasi
```bash
warp-cli status                                                            # Connected
curl -s https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(ip|warp)='   # warp=on
sudo tailscale status                                                      # device online
```

Setelah full-tunnel: FASE1 klaim & FASE2 OTP dua-duanya keluar lewat IP WARP bersih. FASE1
(warm) ≈ secepat direct; overhead cuma di cold TLS handshake pertama.

---