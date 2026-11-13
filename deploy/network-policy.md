# 私网入口策略（06-06-02，待06-08实际配置）

远程路径为个人设备 One Client → Cloudflare 私网 Tunnel → 独立 remote 入口 TCP443 → NGINX mTLS → web。LAN HTTPS 用独立地址和名称，所有局域网设备可直接使用。应用不加账号。WARP 登记只是取得网络连接，持有本相册逐设备证书才取得远程入口授权；新增普通登记设备没有该证书仍须拒绝。

真实域名、LAN/remote IPv4、Tunnel UUID、组织名、账户权限、凭据位置、DNS 托管及客户端版本均尚未配置。已有域名不代表已有 Tunnel。下列空值必须在06-08核验后填写；不自动开通套餐或创建账号。仅接受独立 RFC1918 remote IPv4 和 TCP443；不提供整个 NAS/LAN 网段路由，不使用同IP不同端口作为边界。

## 配置清单

将下列 JSON 保存到部署者选定的受限配置文件，填入真实值。`check-policy` 验证此声明，不能替代 Cloudflare 控制面与 NAS 防火墙的实际证明。

```json
{
  "remote_ipv4": "",
  "remote_port": 443,
  "remote_hostname": "",
  "lan_ipv4": "",
  "lan_hostname": "",
  "route_cidrs": [""],
  "split_include": [""],
  "gateway_allow": [{"ipv4": "", "protocol": "tcp", "port": 443}],
  "gateway_fallback": "block",
  "tls_inspection": false,
  "public_hostname": false,
  "registration_is_authorization": false,
  "connector_networks": ["remote_edge"],
  "remote_edge_members": ["cloudflared", "proxy-remote"],
  "connector_private_egress": [{"ipv4": "", "protocol": "tcp", "port": 443}]
}
```

`route_cidrs` 与 `split_include` 必须各为同一 `remote_ipv4/32`。运行 `sh deploy/manage-device.sh check-policy --policy /ABS/policy.json`，缺值、泛网段、LAN同地址、非443、缺兜底或含web的网络均非零拒绝。此命令不启动cloudflared，不修改网络。

## 必须一起配置的边界

| 层 | 精确允许 | 拒绝与现场证据 |
|---|---|---|
| Tunnel路由 | 一个remote IPv4/32，指向新Tunnel | 无LAN/NAS网段、IPv6泛路由、公开hostname ingress；导出实际路由表 |
| One Client | Traffic and DNS、Include同一/32；私有DNS将remote名称解析到此地址 | 不配置全隧道或整个LAN；记录实际组织、模式、DNS及route |
| Gateway网络规则 | 目的remote IPv4、协议TCP、目的端口443；所需个人登记者 | 较后匹配同一远程用户/设备范围的兜底Block；检查已有更高优先级Allow不会绕过。原本私网默认可通，必须显式阻断其余目标/端口 |
| Gateway TLS | 对该精确目标Do Not Inspect，保留端到端mTLS | 不用Gateway替换客户端证书；不假定Enterprise登记mTLS或MDM设备UUID能力 |
| connector私网出口 | 只remote IPv4 TCP443，及对应已建立连接返回流量 | 拒绝其余RFC1918、LAN监听、web:8000、Docker宿主/网关、NAS管理/22/445；IPv6无业务需求则禁用私网路由并同样阻断，不留IPv6旁路 |
| connector互联网出口 | 当前官方Cloudflare边缘地址UDP7844、已核验DNS解析器TCP/UDP53；若确需控制面HTTPS则另列精确目标 | 不给NAS私网出站通配；更新官方边缘地址时保留控制面连接要求，不能用全放行解决连接问题 |
| 容器网络 | 新独立remote_edge仅cloudflared与proxy-remote；proxy-remote另接已有remote_backend | cloudflared不得接remote_backend（含web）、lan_backend、host或Docker socket；不启IP转发/通用代理 |
| NGINX | remote所有vhost强制证书与CRL，默认SNI拒绝；后端不发布端口 | 保留01既有Host/CSRF/no-store/下载边界，不开私人目录alias或远程/delivery/ |

`compose.yaml`现有backend隔离不等于已经有connector隔离。正式接入时须另行准备只含connector/remote入口的网络及精确端口接线，在控制面策略完成前不启Tunnel；本任务未改Compose/防火墙。Docker出口控制放在实际经过的forward/DOCKER-USER或相应nftables链上，不能仅设置宿主INPUT链后就宣称容器受控。选定私网目标须实际可从remote_edge到达，不能借用免证书LAN端口；若需要宿主地址，只允许该独立remote地址TCP443，阻断宿主其他地址/服务。

06-08须从无相册证书的已登记WARP设备、撤销设备及有效设备分别尝试：remote443、LAN443、web8000、NAS管理/SSH/SMB与地址别名；无证书只能得到TLS/NGINX拒绝，其他服务均不可达。LAN独立入口仍需从普通LAN设备证明可用。保存端口/路由/网络成员及实际拒绝结果，配置文本不能替代这些证据。

## 官方依据

沿用06-RESEARCH的[Cloudflare精确私网路由与Gateway默认放行风险](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/private-net/cloudflared/connect-cidr/)、[Split Tunnel](https://developers.cloudflare.com/learning-paths/replace-vpn/configure-device-agent/split-tunnel-settings/)、[TLS inspection](https://developers.cloudflare.com/learning-paths/secure-internet-traffic/build-http-policies/tls-inspection/)与[NGINX TLS/CRL](https://nginx.org/en/docs/http/ngx_http_ssl_module.html)。账号和四端实际状态以06-08回执为准。
