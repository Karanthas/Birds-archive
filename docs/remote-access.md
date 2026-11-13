# 私人设备远程接入与撤销

本说明交付06-06-02的配置流程。LAN直接打开独立HTTPS地址，无应用账号、设备登记或证书要求；远程使用One Client私网连接和逐设备客户端证书，日常打开相册不增加应用登录。06-06-01的隔离双TLS证明已通过；域名、Tunnel、账号、策略和四端实机均须在06-08另行验收，本页不表示已部署。Windows当前无连接，四端均pending。

## 部署前填写

| 项目 | 当前值与规则 |
|---|---|
| 已有域名、DNS托管及权限 | 未配置，先核验既有域名及Cloudflare DNS权限 |
| LAN/remote主机名 | 未配置；两个不同精确DNS名称，与应用ALLOWED_HOSTS/CSRF Origin匹配 |
| LAN/remote IPv4 | 未配置；独立私网地址，remote仅TCP443，/32 Include |
| Cloudflare组织/Tunnel UUID/凭据 | 未配置；秘密仅留受限文件，不写文档/日志 |
| 服务器TLS与续期 | 未配置；可用已有合法证书或DNS-01，不公开相册HTTP服务作验证 |
| 设备版本、浏览器和首次选择行为 | Windows/macOS/Android/iPhone分别待06-08记录 |

先完成[网络策略](../deploy/network-policy.md)和[cloudflared模板](../deploy/cloudflared.yml.example)的精确路由、Gateway允许/兜底、connector出口与独立网络。禁止把connector接入含web的backend网络；禁止整LAN路由、SSH/SMB/管理页或免证书LAN端口回退。客户端用Traffic and DNS模式，Split Tunnel仅Include remote /32，DNS解析到此精确地址。目标不启Gateway TLS解密。仅WARP登记不授相册权限，不依赖Enterprise登记mTLS或MDM UUID。

服务器证书负责浏览器确认网站身份，客户端CA负责NGINX确认设备身份，两者分开。DNS-01可为不公开网站的已有域名签发证书；DNS API权限应尽量限定该zone，凭据只在选定续期环境中使用。正式配置后记录续期者、到期前检查时间和失败告警；用`openssl x509 -in /ABS/server.crt -noout -dates -checkend 2592000`检查30天余量。证书替换后先`nginx -t`再重载并验证目标Host，不通过则保留旧可用材料。没有DNS权限/证书能力时保持未启用，不自动买套餐。

## 离线逐设备签发

维护者在已有Python3/OpenSSL的可信离线电脑使用[manage-device.sh](../deploy/manage-device.sh)。它不下载依赖、不连接NAS，不执行Docker或Tunnel命令。CA库用0700，仅维护者可访问；根路径必须为绝对、无符号链接/`..`的ASCII `/._-`及字母数字路径。macOS的`/tmp`通常须先解析为`/private/tmp`。可设`BIRDS_DEVICE_PYTHON`为现有Python3解释器绝对路径。

1. 在离线加密介质建立新的专用CA库；在受限临时目录准备不同强随机CA/PFX口令文件，各为当前用户所有的0600普通文件、单行至少16字节。口令不放命令行值、环境变量、聊天、证据或仓库；命令仅传文件路径。
2. 以下`/ABS/...`均是部署者选择的绝对路径占位符，填值后由执行代理运行。首次初始化拒绝覆盖已有CA。默认CA十年、设备90天（可显式1～365天且不超过CA剩余有效期）、CRL七天；长期私钥保存离线，按期签发新身份。

```sh
sh deploy/manage-device.sh init --store /ABS/offline-ca --ca-pass-file /ABS/secret/ca.pass
sh deploy/manage-device.sh issue --store /ABS/offline-ca --ca-pass-file /ABS/secret/ca.pass --device iphone-2026-01 --pfx-pass-file /ABS/secret/iphone.pass
sh deploy/manage-device.sh status --store /ABS/offline-ca
```

3. 每设备用新ID，不能共享PFX或重用ID覆盖。`devices/ID/device.pfx`是加密身份（含该设备私钥及客户端链），`device.crt`为公开证书；OpenSSL index/serial及`registry.json`保存唯一序列、指纹、有效期与撤销状态。工具临时叶私钥在导出后删除（失败也删除），CA私钥加密存于`private/ca.key`且从不发布到NAS。失败签发保留CA/index与失败设备目录供维护者核查，不自动删库/重试同ID；需要重新签发用新ID并核对、撤销失败序列。
4. 将该设备PFX经已授权的安全通道交付到目标设备，口令用另一受控通道输入；安装后删除下载区/消息附件、共享目录及维护机临时交付副本，检查云端同步/备份是否留存。脚本不替用户擦除外部介质；普通unlink不保证SSD物理擦除，因此使用加密临时存储与受控密钥生命周期。CA库、PFX和口令不得进入截图、回执、代码仓库或普通图库备份；离线CA库另做加密备份，恢复时保留index/serial/registry/CRL整套，不能只拷CA私钥后重置序列。
5. 仅公开CA证书与CRL发布到NAS已存在的受限TLS目录：

```sh
sh deploy/manage-device.sh publish --store /ABS/offline-ca --tls-dir /ABS/public-tls
```

`public-tls`必须与CA库分离；工具只写`client-ca.crt`和`clients.crl`，拒绝不同CA覆盖。文件0644仅为NGINX非root读取公开材料，目录及服务器私钥保持部署所需最小权限；不要把整个CA库挂给容器。维护电脑到NAS传输同样只送这两个公开文件；NAS运行时绑定整个TLS目录只读至`/run/birds-tls`，不能单文件bind旧inode后再在宿主replace，CRL更新需在容器内核对SHA。

此方案证明持有获准私钥；PFX可以复制，不是硬件绑定。丢失设备、复制嫌疑或不再使用时撤销该序列并签新ID。旧PFX不能靠删除本机文件而视为撤销。CA泄露需更换CA及全部设备身份，不能靠撤销一个叶证书解决。

## 四端首次、日常与撤销后操作

共同首次步骤：安装官方Cloudflare One Client/Agent，用已核验组织按登记规则完成首次登记，授权系统VPN配置；确认Traffic and DNS及精确Include/DNS生效。此时无相册证书应拒绝访问，再安装本设备PFX并打开remote HTTPS名称。首次登记可需要外部身份认证，但反复浏览不得以每次外部身份登录代替设备方案。首次选证书和系统解锁可能出现，06-08如日常反复弹出则不得宣布D-06通过。

| 系统 | 首次身份安装与浏览器 | 日常使用与下载 | 撤销/换机后 |
|---|---|---|---|
| Windows | 官方One Client手动登记。打开PFX导入“当前用户/个人”证书存储；可由执行代理用`Import-PfxCertificate -FilePath <本设备PFX> -CertStoreLocation Cert:\CurrentUser\My -Password (Read-Host -AsSecureString)`。不勾可导出私钥。用目标Edge/Chrome打开remote名称，选本设备身份（具体版本实际确认） | 连接One Client，重开浏览器直达相册、打开详情并下载；检查保存原图SHA，记录是否重复选证书/登录 | 服务端完成撤销后在原页面重试、新标签及重开浏览器均须拒绝。删除当前用户“个人”中的旧设备身份，换机签新ID；不只删除CA根 |
| macOS | 官方One Client登记。双击PFX或钥匙串访问→文件→导入项目，导入“登录”钥匙串、输入PFX口令；用Safari或选定浏览器首次选本设备身份。不要把客户端CA设为所有网站服务器根“始终信任” | 连接客户端，重开选定浏览器直达、下载并核对SHA；记录钥匙串授权/选择弹窗情况 | 先证实旧连接、新连接均被拒，再在登录钥匙串删除旧身份及对应私钥；换新ID。不能仅清浏览器缓存充当服务端撤销 |
| Android | 官方One Agent登记并授权VPN。按厂商系统“安全/加密与凭据/安装证书/VPN和应用证书”导入本设备PFX，输入口令，必要时设系统屏幕锁；Chrome请求证书时选此身份。厂商菜单及Chrome版本须实录 | 连接客户端，重开Chrome直达、下载到设备并核对SHA；记录首次选择/日常重复交互，不能用Pixel说明替代其他厂商实机 | 服务端撤销后保持原标签和新开均拒绝；在用户凭据中移除旧身份、清理PFX交付副本，换新ID |
| iPhone（iOS） | 官方One Agent登记、授权VPN。通过受控交付打开本设备PFX，按系统“已下载描述文件”或设置→通用→VPN与设备管理安装身份并输入口令，按实际iOS提示完成；Safari访问remote名称。不能把其他iOS浏览器自动算通过 | 连接客户端，重开Safari直接打开、保存下载并核对SHA；记录系统版本、证书提示与下载位置。系统解锁与首次配置区别于日常外部身份登录 | 服务端撤销后原Safari连接、新标签及重开都拒绝；在描述文件/设备管理删除旧身份配置并清理交付副本，换新ID |

客户端身份安装不应解除浏览器对服务器证书/Host的验证，不点击忽略证书错误。若目标版本不能安装默认现代PKCS#12或反复外部认证，记录具体失败并保持该端pending，不自动降级PFX加密、关闭mTLS或换公开地址。

## 撤销、CRL续期与实际生效门

离线库的`revoke`只生成撤销材料，输出`offline_revocation_material_updated`及`live_revocation_verified:false`。它不表示入口已生效。`status`也只是离线登记表，不探测在线服务器。

```sh
sh deploy/manage-device.sh revoke --store /ABS/offline-ca --ca-pass-file /ABS/secret/ca.pass --device iphone-2026-01
sh deploy/manage-device.sh publish --store /ABS/offline-ca --tls-dir /ABS/public-tls
```

由唯一NAS执行者把公开文件原子发布到挂载的TLS目录、核对容器内外CRL SHA，执行实际`nginx -t -c /tmp/nginx.conf`，通过后执行`nginx -c /tmp/nginx.conf -s reload`。失败则保持远程入口关闭/隔离，修正后重新验证；不能把旧CRL仍可用写成已撤销。

NGINX reload会让旧worker保留现有连接；模板的`worker_shutdown_timeout 5s`给旧worker关闭上界，不能只看reload返回0。必须提前记录master/worker PID和启动身份，在同一A客户端TLS keepalive socket读过有效响应并保持连接，再更新CRL/reload；观察旧worker退出与原socket EOF/关闭，并断言原socket之后不能读取照片。到期仍有旧worker或旧连接可读则撤销失败，保持入口隔离，由唯一执行者对已核验的自有remote容器执行有界停止/重启后重测，不能杀其他NAS服务。撤销传播有最多约5秒的排空窗口，不能宣称更新文件那一刻旧连接即不可读。

同时保留`ssl_session_cache off`、`ssl_session_tickets off`和`ssl_early_data off`，重载后用被撤销A新TLS连接验证拒绝，用仍有效B确认可以读取同一夹具SHA。仅`openssl verify`证明离线CRL不能替代这次真实NGINX新/旧连接证明；HTTP200空页、自动重连后读成功、其他端身份或过期证书均不能冒充A撤销成功。实验使用全新隔离CA/两叶，旧2026-10-29两天期实验身份已过期不能复用。

CRL默认七天。维护者在第五天前于离线库运行下列刷新、再按同一公开材料发布/`nginx -t`/reload/连接关闭步骤完成更新；检查`openssl crl -in /ABS/clients.crl -noout -lastupdate -nextupdate`。到期无有效CRL时保留拒绝服务，不关闭CRL验证。刷新不撤销其他设备，但重载可能中断连接。

```sh
sh deploy/manage-device.sh refresh-crl --store /ABS/offline-ca --ca-pass-file /ABS/secret/ca.pass
```

设备证书更新同样用新ID，验证新身份后撤销旧身份并清理旧PFX；临近到期不是自动续签许可。停用设备可同时撤销One Client登记，但那本身不能替代客户端证书撤销。

## 验收分工与官方依据

本地`DevicePolicyTests`仅覆盖唯一签发/PFX、CRL离线验证、公开材料发布和错误配置拒绝。新真实NAS撤销结果写06-06-02回执；全06-06摘要须等该结果。06-08按四端逐项记录OS/浏览器/One Client版本、首次登记与证书安装、日常重开/下载SHA、无证书/已撤销拒绝及LAN/后端/NAS服务旁路拒绝；目前四端均未验收。不重跑已过七项TLS、已有同JPEG归档或模型评测。

四端说明沿用06-RESEARCH既有官方依据：[One Client手动部署](https://developers.cloudflare.com/cloudflare-one/team-and-resources/devices/cloudflare-one-client/deployment/manual-deployment/)、[Windows PFX导入](https://learn.microsoft.com/en-us/powershell/module/pki/import-pfxcertificate)、[Apple身份分发](https://support.apple.com/guide/deployment/distribute-certificates-depcdc9a6a3f/1/web/1.0)、[Android用户凭据](https://support.google.com/pixelphone/answer/2844832?hl=en)、[Chromium客户端证书存储](https://chromium.googlesource.com/chromium/src/+/HEAD/net/data/ssl/chrome_root_store/faq.md)、[iPhone证书与Safari](https://support.apple.com/guide/iphone/digital-certificates-and-encrypted-websites-iph1b914c6d4/ios)、[DNS-01](https://letsencrypt.org/docs/challenge-types/)。工具使用既有OpenSSL 3.3.1，并最小核对[OpenSSL CA/CRL](https://docs.openssl.org/3.3/man1/openssl-ca/)及[PKCS#12](https://docs.openssl.org/3.3/man1/openssl-pkcs12/)参数；真实客户端兼容性仍以06-08为准。
