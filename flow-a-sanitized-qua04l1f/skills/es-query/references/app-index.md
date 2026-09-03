# 行情应用 Deployment 索引

> 来源：应用.csv，共 143 个 Deployment，142 个应用（HK）；新加坡源.csv，共 136 个 Deployment（SG）

## MAS 源说明

> Kibana 地址：`https://mas-kibana-prod.hszq8.com/app`

当前未维护完整的 MAS 应用 -> Deployment 清单。查询 MAS 时：

- 优先使用用户明确给出的 hostname / Deployment
- 若应用满足已知规则，可按规则推导 hostname
- 若无法由现有规则推出 hostname，按阻塞处理，提示补充映射

已知映射规则：

| 应用名（app_name） | 目标地域 | hostname 前缀（用于 `--host`） |
|---|---|---|
| `hq-interface-aggregation-sgp` | 新加坡 | `hq-interface-aggregation-sgp-mas-alisgp` |
| `hq-interface-aggregation-sgp` | 香港 | `hq-interface-aggregation-sgp-mas-alihk` |
| `hq-interface-general-mas-base` | 香港 | `hq-interface-general-mas-base-mas-alihk-product-v2` |

## HK 源（默认）应用 → agent.hostname 映射

> ES 地址：`https://easysearch-hk.hszq8.com`，hostname 后缀规律：`-hkeq-product-tomcat`

ES 查询时 `--host` 参数传 **Deployment** 列的值（即 `agent.hostname`）；不带 `*` 时精确匹配 `agent.hostname`，带 `*` 时前缀通配 `agent.hostname.keyword`。

| 应用名（app_name） | Deployment（agent.hostname） | Cluster | Replicas |
|---|---|---|---|
| `hq-admin` | `hq-admin-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-admin-global-config` | `hq-admin-global-config-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-ashares-stock-acceptor` | `hq-ashares-stock-acceptor-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-ashares-stock-processor` | `hq-ashares-stock-processor-hkeq-product-tomcat` | hshq-hkeq-prod | 3 |
| `hq-calculator-mem` | `hq-calculator-mem-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-calculator-mem-consolidated` | `hq-calculator-mem-consolidated-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-calculator-option` | `hq-calculator-option-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-calculator-realtime` | `hq-calculator-realtime-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-calculator-realtime-consolidated` | `hq-calculator-realtime-consolidated-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-calculator-scheduler` | `hq-calculator-scheduler-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-calculator-scheduler-hk-public` | `hq-calculator-scheduler-hk-public-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-calculator-scheduler-us-public` | `hq-calculator-scheduler-us-public-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-calculator-scheduler-us-sectors-con` | `hq-calculator-scheduler-us-sectors-con-sgpeq-prod-tomcat` | hshq-hkeq-prod | 2 |
| `hq-calculator-usconsolidated-scheduler` | `hq-calculator-usconsolidated-scheduler-sgpeq-prod-tomcat` | hshq-hkeq-prod | 1 |
| `hq-http-gateway-hk` | `hq-http-gateway-hk-hkeq-product-tomcat` | hshq-hkeq-prod | 3 |
| `hq-interface` | `hq-interface-hkeq-product-tomcat` | hshq-hkeq-prod | 10 |
| `hq-interface-ashare-fin` | `hq-interface-ashare-fin-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-interface-general-ashare-base` | `hq-interface-general-ashare-base-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-interface-general-global-base` | `hq-interface-general-global-base-hkeq-product-tomcat` | hshq-hkeq-prod | 4 |
| `hq-interface-global-config` | `hq-interface-global-config-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-interface-hk-base-sgp` | `hq-interface-hk-base-sgp-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-interface-open-ashare` | `hq-interface-open-ashare-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-interface-open-global` | `hq-interface-open-global-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-interface-open-hk-sgp` | `hq-interface-open-hk-sgp-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-interface-open-us-sgp` | `hq-interface-open-us-sgp-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-interface-push-ashare` | `hq-interface-push-ashare-hkeq-product-tomcat` | hshq-hkeq-prod | 4 |
| `hq-interface-push-global` | `hq-interface-push-global-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-interface-push-hk-sgp` | `hq-interface-push-hk-sgp-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-interface-push-us-sgp` | `hq-interface-push-us-sgp-hkeq-product-tomcat` | hshq-hkeq-prod | 12 |
| `hq-interface-socket` | `hq-interface-socket-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-interface-socket-open` | `hq-interface-socket-open-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-interface-socket-open-hk` | `hq-interface-socket-open-hk-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-interface-socket-open-us` | `hq-interface-socket-open-us-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-interface-unified-control` | `hq-interface-unified-control-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-interface-us-base` | `hq-interface-us-base-sgpeq-prod-tomcat` | hshq-hkeq-prod | 1 |
| `hq-interface-us-base-sgp` | `hq-interface-us-base-sgp-hk-sgpeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-interface-web` | `hq-interface-web-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-interface-web-us-base` | `hq-interface-web-us-base-sgpeq-prod-tomcat` | hshq-hkeq-prod | 1 |
| `hq-mem-ashare` | `hq-mem-ashare-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-mem-global` | `hq-mem-global-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-mem-hk` | `hq-mem-hk-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-mem-hk-future` | `hq-mem-hk-future-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-mem-us` | `hq-mem-us-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-mem-us-consolidated` | `hq-mem-us-consolidated-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-mem-us-future` | `hq-mem-us-future-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-mem-us-option` | `hq-mem-us-option-hk-sgpeq-product-tomcat` | hshq-hkeq-prod | 4 |
| `hq-mem-us-option-delay` | `hq-mem-us-option-delay-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-mem-us-otc` | `hq-mem-us-otc-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-open-push-us` | `hq-open-push-us-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-receiver-ashare-szfiu` | `hq-receiver-ashare-szfiu-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-crypto-hashkey` | `hq-receiver-crypto-hashkey-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-receiver-crypto-hashkey-proxy` | `hq-receiver-crypto-hashkey-proxy-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-crypto-hashkey-proxy` | `hq-receiver-crypto-hashkey-proxy-product-tomcat` | hshq-hkaws-k8s-prod1 | 1 |
| `hq-receiver-data-process-server` | `hq-receiver-data-process-server-hkeq-product-tomcat` | hshq-hkeq-prod | 3 |
| `hq-receiver-depthbook-ice` | `hq-receiver-depthbook-ice-hkeq-product-tomcat` | hshq-hkeq-prod | 10 |
| `hq-receiver-depthbook-ice-proxy-v2` | `hq-receiver-depthbook-ice-proxy-v2-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-depthbook-totalview-ice` | `hq-receiver-depthbook-totalview-ice-hkeq-product-tomcat` | hshq-hkeq-prod | 10 |
| `hq-receiver-depthbook-totalview-ice-hot` | `hq-receiver-depthbook-totalview-ice-hot-sgpeq-prod-tomcat` | hshq-hkeq-prod | 2 |
| `hq-receiver-depthbook-totalview-ice-proxy` | `hq-receiver-depthbook-totalview-ice-proxy-hkeq-prod-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-hk-fiu-delay` | `hq-receiver-hk-fiu-delay-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-hk-future-ice` | `hq-receiver-hk-future-ice-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-hk-future-ice-proxy` | `hq-receiver-hk-future-ice-proxy-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-hk-szfiu` | `hq-receiver-hk-szfiu-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-totalview-ice-proxy-v2` | `hq-receiver-totalview-ice-proxy-v2-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-delay` | `hq-receiver-us-delay-hkeq-product-tomcat` | hshq-hkeq-prod | 4 |
| `hq-receiver-us-fiu` | `hq-receiver-us-fiu-hkeq-product-tomcat` | hshq-hkeq-prod | 12 |
| `hq-receiver-us-fiu-backup` | `hq-receiver-us-fiu-backup-hkeq-product-tomcat` | hshq-hkeq-prod | 8 |
| `hq-receiver-us-fiu-hot` | `hq-receiver-us-fiu-hot-hkeq-prod-tomcat` | hshq-hkeq-prod | 4 |
| `hq-receiver-us-fiu-hot-backup` | `hq-receiver-us-fiu-hot-backup-hkeq-prod-tomcat` | hshq-hkeq-prod | 4 |
| `hq-receiver-us-fiu-proxy` | `hq-receiver-us-fiu-proxy-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-future-fiu` | `hq-receiver-us-future-fiu-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-future-ice` | `hq-receiver-us-future-ice-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-receiver-us-future-ice-hot` | `hq-receiver-us-future-ice-hot-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-future-ice-proxy-v2` | `hq-receiver-us-future-ice-proxy-v2-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-ice` | `hq-receiver-us-ice-hkeq-product-tomcat` | hshq-hkeq-prod | 12 |
| `hq-receiver-us-ice-backup` | `hq-receiver-us-ice-backup-hkeq-product-tomcat` | hshq-hkeq-prod | 8 |
| `hq-receiver-us-ice-backup-hot` | `hq-receiver-us-ice-backup-hot-hkeq-prod-tomcat` | hshq-hkeq-prod | 4 |
| `hq-receiver-us-ice-hot` | `hq-receiver-us-ice-hot-hkeq-product-tomcat` | hshq-hkeq-prod | 4 |
| `hq-receiver-us-ice-proxy` | `hq-receiver-us-ice-proxy-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-ice-proxy-amex-v2` | `hq-receiver-us-ice-proxy-amex-v2-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-ice-proxy-nasdaq` | `hq-receiver-us-ice-proxy-nasdaq-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-ice-proxy-nyse` | `hq-receiver-us-ice-proxy-nyse-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-index-fiu-v2` | `hq-receiver-us-index-fiu-v2-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-index-ice` | `hq-receiver-us-index-ice-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-receiver-us-index-ice-proxy` | `hq-receiver-us-index-ice-proxy-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-option-gth-ice` | `hq-receiver-us-option-gth-ice-hkeq-product-tomcat` | hshq-hkeq-prod | 3 |
| `hq-receiver-us-option-ice` | `hq-receiver-us-option-ice-hkeq-product-tomcat` | hshq-hkeq-prod | 16 |
| `hq-receiver-us-option-ice-proxy` | `hq-receiver-us-option-ice-proxy-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-option-ice-proxy-v2` | `hq-receiver-us-option-ice-proxy-v2-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-otc-ice` | `hq-receiver-us-otc-ice-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-receiver-us-otc-ice-proxy-v2` | `hq-receiver-us-otc-ice-proxy-v2-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-receiver-us-overnight-boats-ice` | `hq-receiver-us-overnight-boats-ice-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-receiver-us-overnight-boats-proxy` | `hq-receiver-us-overnight-boats-proxy-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-scheduler` | `hq-scheduler-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-scheduler-ashare-archive` | `hq-scheduler-ashare-archive-hk-sgpeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-scheduler-global-archive` | `hq-scheduler-global-archive-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-scheduler-hk-archive` | `hq-scheduler-hk-archive-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-scheduler-us-archive` | `hq-scheduler-us-archive-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-scheduler-us-public` | `hq-scheduler-us-public-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-service` | `hq-service-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-service-async` | `hq-service-async-hkeq-product-tomcat` | hshq-hkeq-prod | 4 |
| `hq-service-base` | `hq-service-base-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-service-base-global-config` | `hq-service-base-global-config-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-service-base-history-consolidated` | `hq-service-base-history-consolidated-hkeq-product-tomcat` | hshq-hkeq-prod | 3 |
| `hq-service-base-history-global` | `hq-service-base-history-global-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-service-base-history-hk` | `hq-service-base-history-hk-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-service-base-history-option` | `hq-service-base-history-option-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-service-base-history-otc` | `hq-service-base-history-otc-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-service-base-history-us` | `hq-service-base-history-us-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-service-base-us` | `hq-service-base-us-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-service-basedata` | `hq-service-basedata-hkeq-product-tomcat` | hshq-hkeq-prod | 5 |
| `hq-service-buz-fin-engine` | `hq-service-buz-fin-engine-hkeq-beta-tomcat` | hshq-hkeq-prod | 2 |
| `hq-service-buz-fin-rule-engine` | `hq-service-buz-fin-rule-engine-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-service-buz-us-fin-engine` | `hq-service-buz-us-fin-engine-hkeq-beta-tomcat` | hshq-hkeq-prod | 2 |
| `hq-service-consolidated` | `hq-service-consolidated-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-service-extend` | `hq-service-extend-hkeq-product-tomcat` | hshq-hkeq-prod | 10 |
| `hq-service-fundamental` | `hq-service-fundamental-hkeq-product-tomcat` | hshq-hkeq-prod | 4 |
| `hq-service-history` | `hq-service-history-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hq-service-instant` | `hq-service-instant-hkeq-product-tomcat` | hshq-hkeq-prod | 10 |
| `hq-service-static-data` | `hq-service-static-data-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-service-unified` | `hq-service-unified-hk-sgpeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-service-usoptioncalcore` | `hq-service-usoptioncalcore-aws-prod-tomcat` | hshq-hkaws-k8s-prod1 | 3 |
| `hq-static-data-admin` | `hq-static-data-admin-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-static-data-base-hk` | `hq-static-data-base-hk-hkeq-product-koupleless` | hshq-hkeq-prod | 2 |
| `hq-static-data-base-us` | `hq-static-data-base-us-hkeq-product-koupleless` | hshq-hkeq-prod | 4 |
| `hq-static-data-interface` | `hq-static-data-interface-hkeq-product-koupleless` | hshq-hkeq-prod | 2 |
| `hq-static-data-scheduler` | `hq-static-data-scheduler-hkeq-product-koupleless` | hshq-hkeq-prod | 3 |
| `hq-test` | `hq-test-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-thirdparty-open` | `hq-thirdparty-open-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-thirdparty-quant` | `hq-thirdparty-quant-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |
| `hq-thirdparty-quant-api` | `hq-thirdparty-quant-api-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-unify-open-push-ashare` | `hq-unify-open-push-ashare-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-unify-open-push-hk` | `hq-unify-open-push-hk-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hq-unify-open-push-us` | `hq-unify-open-push-us-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hs-gl-socket-gateway-server` | `hs-gl-socket-gateway-server-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hs-gl-socket-gateway-server-hq-unify` | `hs-gl-socket-gateway-server-hq-unify-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hs-gl-socket-gateway-server-snappy` | `hs-gl-socket-gateway-server-snappy-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hs-gl-subscription-server` | `hs-gl-subscription-server-hkeq-product-tomcat` | hshq-hkeq-prod | 3 |
| `hs-gl-subscription-server-hq-unify` | `hs-gl-subscription-server-hq-unify-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hs-hk-admin-dubbo-base-hq-1` | `hs-hk-admin-dubbo-base-hq-1-hkeq-product-koupleless` | hshq-hkeq-prod | 1 |
| `hs-hk-stock-hq-acceptor` | `hs-hk-stock-hq-acceptor-hkeq-product-tomcat` | hshq-hkeq-prod | 2 |
| `hs-hk-stock-processor` | `hs-hk-stock-processor-hkeq-product-tomcat` | hshq-hkeq-prod | 6 |
| `hs-hq-xxl-job-admin` | `hs-hq-xxl-job-admin-hkeq-product-tomcat` | hshq-hkeq-prod | 1 |

---

## SG 源应用 → agent.hostname 映射

> ES 地址：`https://easysearch-sg.hszq8.com`，Cluster：`hshq-vsggds-prod`，hostname 后缀规律：`-hk-sgpeq-product-tomcat`

| 应用名（app_name） | Deployment（agent.hostname） | Replicas |
|---|---|---|
| `arch-data-sync-consumer-hk-hq` | `arch-data-sync-consumer-hk-hq-hk-sgpeq-product-tomcat` | 1 |
| `hq-admin` | `hq-admin-hk-sgpeq-product-tomcat` | 1 |
| `hq-ashares-stock-acceptor` | `hq-ashares-stock-acceptor-hk-sgpeq-product-tomcat` | 1 |
| `hq-ashares-stock-processor` | `hq-ashares-stock-processor-hk-sgpeq-product-tomcat` | 2 |
| `hq-calculator-mem-consolidated` | `hq-calculator-mem-consolidated-hk-sgpeq-product-tomcat` | 1 |
| `hq-calculator-mem` | `hq-calculator-mem-hk-sgpeq-product-tomcat` | 1 |
| `hq-calculator-option` | `hq-calculator-option-hk-sgpeq-product-tomcat` | 4 |
| `hq-calculator-realtime` | `hq-calculator-realtime-hk-sgpeq-product-tomcat` | 1 |
| `hq-calculator-scheduler-hk-public` | `hq-calculator-scheduler-hk-public-hk-sgpeq-product-tomcat` | 2 |
| `hq-calculator-scheduler` | `hq-calculator-scheduler-hk-sgpeq-product-tomcat` | 1 |
| `hq-calculator-scheduler-us-public` | `hq-calculator-scheduler-us-public-hk-sgpeq-product-tomcat` | 1 |
| `hq-calculator-scheduler-us-sectors-con` | `hq-calculator-scheduler-us-sectors-con-sgpeq-prod-tomcat` | 2 |
| `hq-calculator-usconsolidated-scheduler` | `hq-calculator-usconsolidated-scheduler-sgpeq-prod-tomcat` | 1 |
| `hq-http-gateway-hk` | `hq-http-gateway-hk-hk-sgpeq-product-tomcat` | 2 |
| `hq-interface-ashare-fin` | `hq-interface-ashare-fin-hk-sgpeq-product-tomcat` | 2 |
| `hq-interface-general-ashare-base` | `hq-interface-general-ashare-base-hk-sgpeq-product-tomcat` | 2 |
| `hq-interface-general-global-base` | `hq-interface-general-global-base-hk-sgpeq-product-tomcat` | 2 |
| `hq-interface-hk-base-sgp` | `hq-interface-hk-base-sgp-hk-sgpeq-product-tomcat` | 2 |
| `hq-interface` | `hq-interface-hk-sgpeq-product-tomcat` | 6 |
| `hq-interface-open-ashare` | `hq-interface-open-ashare-hk-sgpeq-product-tomcat` | 6 |
| `hq-interface-open-global` | `hq-interface-open-global-hk-sgpeq-product-tomcat` | 2 |
| `hq-interface-open-hk-sgp` | `hq-interface-open-hk-sgp-hk-sgpeq-product-tomcat` | 6 |
| `hq-interface-open-us-sgp` | `hq-interface-open-us-sgp-hk-sgpeq-product-tomcat` | 5 |
| `hq-interface-push-ashare` | `hq-interface-push-ashare-hk-sgpeq-product-tomcat` | 4 |
| `hq-interface-push-global` | `hq-interface-push-global-hk-sgpeq-product-tomcat` | 2 |
| `hq-interface-push-hk-sgp` | `hq-interface-push-hk-sgp-hk-sgpeq-product-tomcat` | 6 |
| `hq-interface-push-us-sgp` | `hq-interface-push-us-sgp-hk-sgpeq-product-tomcat` | 12 |
| `hq-interface-socket` | `hq-interface-socket-hk-sgpeq-product-tomcat` | 6 |
| `hq-interface-socket-open-hk` | `hq-interface-socket-open-hk-hk-sgpeq-prod-tomcat` | 2 |
| `hq-interface-socket-open` | `hq-interface-socket-open-hk-sgpeq-product-tomcat` | 2 |
| `hq-interface-socket-open-us` | `hq-interface-socket-open-us-sgpeq-prod-tomcat` | 2 |
| `hq-interface-us-base-sgp` | `hq-interface-us-base-sgp-hk-sgpeq-product-tomcat` | 2 |
| `hq-interface-web` | `hq-interface-web-hk-sgpeq-product-tomcat` | 2 |
| `hq-mem-ashare` | `hq-mem-ashare-hk-sgpeq-product-tomcat` | 6 |
| `hq-mem-global` | `hq-mem-global-hk-sgpeq-product-tomcat` | 2 |
| `hq-mem-hk-future` | `hq-mem-hk-future-hk-sgpeq-product-tomcat` | 6 |
| `hq-mem-hk` | `hq-mem-hk-hk-sgpeq-product-tomcat` | 6 |
| `hq-mem-us-consolidated` | `hq-mem-us-consolidated-hk-sgpeq-product-tomcat` | 6 |
| `hq-mem-us-future` | `hq-mem-us-future-hk-sgpeq-product-tomcat` | 6 |
| `hq-mem-us` | `hq-mem-us-hk-sgpeq-product-tomcat` | 6 |
| `hq-mem-us-option-delay` | `hq-mem-us-option-delay-hk-sgpeq-product-tomcat` | 2 |
| `hq-mem-us-option` | `hq-mem-us-option-hk-sgpeq-product-tomcat` | 4 |
| `hq-mem-us-otc` | `hq-mem-us-otc-hk-sgpeq-product-tomcat` | 6 |
| `hq-open-push-us` | `hq-open-push-us-hk-sgpeq-product-tomcat` | 6 |
| `hq-receiver-ashare-szfiu` | `hq-receiver-ashare-szfiu-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-crypto-hashkey` | `hq-receiver-crypto-hashkey-hk-sgpeq-product-tomcat` | 2 |
| `hq-receiver-crypto-hashkey-proxy` | `hq-receiver-crypto-hashkey-proxy-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-data-process-server` | `hq-receiver-data-process-server-hk-sgpeq-product-tomcat` | 3 |
| `hq-receiver-depthbook-ice` | `hq-receiver-depthbook-ice-hk-sgpeq-product-tomcat` | 10 |
| `hq-receiver-depthbook-ice-proxy` | `hq-receiver-depthbook-ice-proxy-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-depthbook-ice-proxy-v2` | `hq-receiver-depthbook-ice-proxy-v2-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-depthbook-totalview-ice` | `hq-receiver-depthbook-totalview-ice-hk-sgpeq-product-tomcat` | 10 |
| `hq-receiver-depthbook-totalview-ice-hot` | `hq-receiver-depthbook-totalview-ice-hot-sgpeq-prod-tomcat` | 2 |
| `hq-receiver-depthbook-totalview-ice-proxy` | `hq-receiver-depthbook-totalview-ice-proxy-sgpeq-prod-tomcat` | 1 |
| `hq-receiver-hk-fiu-delay` | `hq-receiver-hk-fiu-delay-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-hk-future-ice` | `hq-receiver-hk-future-ice-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-hk-future-ice-proxy` | `hq-receiver-hk-future-ice-proxy-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-hk-szfiu` | `hq-receiver-hk-szfiu-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-totalview-ice-proxy-v2` | `hq-receiver-totalview-ice-proxy-v2-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-delay` | `hq-receiver-us-delay-hk-sgpeq-product-tomcat` | 4 |
| `hq-receiver-us-fiu-backup` | `hq-receiver-us-fiu-backup-hk-sgpeq-product-tomcat` | 8 |
| `hq-receiver-us-fiu` | `hq-receiver-us-fiu-hk-sgpeq-product-tomcat` | 12 |
| `hq-receiver-us-fiu-hot-backup` | `hq-receiver-us-fiu-hot-backup-sgpeq-prod-tomcat` | 4 |
| `hq-receiver-us-fiu-hot` | `hq-receiver-us-fiu-hot-sgpeq-prod-tomcat` | 4 |
| `hq-receiver-us-fiu-proxy` | `hq-receiver-us-fiu-proxy-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-future-fiu` | `hq-receiver-us-future-fiu-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-future-ice` | `hq-receiver-us-future-ice-hk-sgpeq-product-tomcat` | 6 |
| `hq-receiver-us-future-ice-hot` | `hq-receiver-us-future-ice-hot-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-future-ice-proxy` | `hq-receiver-us-future-ice-proxy-sgpeq-prod-tomcat` | 1 |
| `hq-receiver-us-future-ice-proxy-v2` | `hq-receiver-us-future-ice-proxy-v2-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-ice-backup` | `hq-receiver-us-ice-backup-hk-sgpeq-product-tomcat` | 8 |
| `hq-receiver-us-ice-backup-hot` | `hq-receiver-us-ice-backup-hot-sgpeq-prod-tomcat` | 2 |
| `hq-receiver-us-ice` | `hq-receiver-us-ice-hk-sgpeq-product-tomcat` | 10 |
| `hq-receiver-us-ice-hot` | `hq-receiver-us-ice-hot-sgpeq-prod-tomcat` | 4 |
| `hq-receiver-us-ice-proxy-amex-v2` | `hq-receiver-us-ice-proxy-amex-v2-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-ice-proxy` | `hq-receiver-us-ice-proxy-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-ice-proxy-nasdaq` | `hq-receiver-us-ice-proxy-nasdaq-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-ice-proxy-nyse` | `hq-receiver-us-ice-proxy-nyse-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-index-fiu-v2` | `hq-receiver-us-index-fiu-v2-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-index-ice` | `hq-receiver-us-index-ice-hk-sgpeq-product-tomcat` | 2 |
| `hq-receiver-us-index-ice-proxy` | `hq-receiver-us-index-ice-proxy-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-option-gth-ice` | `hq-receiver-us-option-gth-ice-hk-sgpeq-product-tomcat` | 3 |
| `hq-receiver-us-option-ice` | `hq-receiver-us-option-ice-hk-sgpeq-product-tomcat` | 16 |
| `hq-receiver-us-option-ice-proxy` | `hq-receiver-us-option-ice-proxy-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-option-ice-proxy-v2` | `hq-receiver-us-option-ice-proxy-v2-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-otc-ice` | `hq-receiver-us-otc-ice-hk-sgpeq-product-tomcat` | 6 |
| `hq-receiver-us-otc-ice-proxy` | `hq-receiver-us-otc-ice-proxy-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-otc-ice-proxy-v2` | `hq-receiver-us-otc-ice-proxy-v2-hk-sgpeq-product-tomcat` | 1 |
| `hq-receiver-us-overnight-boats-ice` | `hq-receiver-us-overnight-boats-ice-hk-sgpeq-product-tomcat` | 2 |
| `hq-receiver-us-overnight-boats-proxy` | `hq-receiver-us-overnight-boats-proxy-sgpeq-product-tomcat` | 1 |
| `hq-scheduler-ashare-archive` | `hq-scheduler-ashare-archive-hk-sgpeq-product-tomcat` | 2 |
| `hq-scheduler-global-archive` | `hq-scheduler-global-archive-hk-sgpeq-product-tomcat` | 2 |
| `hq-scheduler-hk-archive` | `hq-scheduler-hk-archive-hk-sgpeq-product-tomcat` | 2 |
| `hq-scheduler` | `hq-scheduler-hk-sgpeq-product-tomcat` | 1 |
| `hq-scheduler-us-archive` | `hq-scheduler-us-archive-hk-sgpeq-product-tomcat` | 2 |
| `hq-scheduler-us-public` | `hq-scheduler-us-public-hk-sgpeq-product-tomcat` | 1 |
| `hq-service-async` | `hq-service-async-hk-sgpeq-product-tomcat` | 4 |
| `hq-service-base-history-consolidated` | `hq-service-base-history-consolidated-hk-sgpeq-product-tomcat` | 3 |
| `hq-service-base-history-global` | `hq-service-base-history-global-hk-sgpeq-product-tomcat` | 2 |
| `hq-service-base-history-hk` | `hq-service-base-history-hk-hk-sgpeq-product-tomcat` | 2 |
| `hq-service-base-history-option` | `hq-service-base-history-option-hk-sgpeq-product-tomcat` | 2 |
| `hq-service-base-history-otc` | `hq-service-base-history-otc-hk-sgpeq-product-tomcat` | 2 |
| `hq-service-base-history-us` | `hq-service-base-history-us-hk-sgpeq-product-tomcat` | 2 |
| `hq-service-base` | `hq-service-base-hk-sgpeq-product-tomcat` | 2 |
| `hq-service-base-us` | `hq-service-base-us-hk-sgpeq-product-tomcat` | 2 |
| `hq-service-basedata` | `hq-service-basedata-hk-sgpeq-product-tomcat` | 4 |
| `hq-service-buz-fin-engine` | `hq-service-buz-fin-engine-hk-sgpeq-product-tomcat` | 2 |
| `hq-service-buz-fin-rule-engine` | `hq-service-buz-fin-rule-engine-hk-sgpeq-product-tomcat` | 1 |
| `hq-service-buz-us-fin-engine` | `hq-service-buz-us-fin-engine-hk-sgpeq-product-tomcat` | 2 |
| `hq-service-consolidated` | `hq-service-consolidated-hk-sgpeq-product-tomcat` | 6 |
| `hq-service-extend` | `hq-service-extend-hk-sgpeq-product-tomcat` | 6 |
| `hq-service-fundamental` | `hq-service-fundamental-hk-sgpeq-product-tomcat` | 4 |
| `hq-service-history` | `hq-service-history-hk-sgpeq-product-tomcat` | 6 |
| `hq-service` | `hq-service-hk-sgpeq-product-tomcat` | 6 |
| `hq-service-instant` | `hq-service-instant-hk-sgpeq-product-tomcat` | 6 |
| `hq-service-static-data` | `hq-service-static-data-hk-sgpeq-product-tomcat` | 1 |
| `hq-service-unified` | `hq-service-unified-hk-sgpeq-product-tomcat` | 2 |
| `hq-service-usoptioncalcore` | `hq-service-usoptioncalcore-hk-sgpeq-product-tomcat` | 6 |
| `hq-static-data-base-hk` | `hq-static-data-base-hk-hk-sgpeq-product-koupleless` | 2 |
| `hq-static-data-base-us` | `hq-static-data-base-us-hk-sgpeq-product-koupleless` | 4 |
| `hq-static-data-interface` | `hq-static-data-interface-hk-sgpeq-product-koupleless` | 2 |
| `hq-static-data-scheduler` | `hq-static-data-scheduler-hk-sgpeq-product-koupleless` | 3 |
| `hq-thirdparty-open` | `hq-thirdparty-open-hk-sgpeq-product-tomcat` | 2 |
| `hq-thirdparty-quant-api` | `hq-thirdparty-quant-api-hk-sgpeq-product-tomcat` | 2 |
| `hq-thirdparty-quant` | `hq-thirdparty-quant-hk-sgpeq-product-tomcat` | 1 |
| `hq-unify-open-push-ashare` | `hq-unify-open-push-ashare-hk-sgpeq-product-tomcat` | 2 |
| `hq-unify-open-push-hk` | `hq-unify-open-push-hk-hk-sgpeq-product-tomcat` | 2 |
| `hq-unify-open-push-us` | `hq-unify-open-push-us-hk-sgpeq-product-tomcat` | 6 |
| `hs-gl-socket-gateway-server` | `hs-gl-socket-gateway-server-hk-sgpeq-product-tomcat` | 2 |
| `hs-gl-socket-gateway-server-hq-unify` | `hs-gl-socket-gateway-server-hq-unify-hk-sgpeq-product-tomcat` | 2 |
| `hs-gl-socket-gateway-server-snappy` | `hs-gl-socket-gateway-server-snappy-hk-sgpeq-product-tomcat` | 2 |
| `hs-gl-subscription-server` | `hs-gl-subscription-server-hk-sgpeq-product-tomcat` | 3 |
| `hs-gl-subscription-server-hq-unify` | `hs-gl-subscription-server-hq-unify-hk-sgpeq-product-tomcat` | 2 |
| `hs-hk-stock-hq-acceptor` | `hs-hk-stock-hq-acceptor-hk-sgpeq-product-tomcat` | 2 |
| `hs-hk-stock-processor` | `hs-hk-stock-processor-hk-sgpeq-product-tomcat` | 6 |
| `hs-hq-xxl-job-admin` | `hs-hq-xxl-job-admin-hk-sgpeq-product-tomcat` | 1 |
