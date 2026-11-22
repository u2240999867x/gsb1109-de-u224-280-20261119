-- 窗口报表：内层 w 按设备分区、按秒数排序（ROWS：当前行 + 上一行）求 rolling，
-- 外层同名 w 按机构分区、按秒数 RANGE（当前秒及前一秒）求 rolling 总和。
-- 日常展示按 id 逆序，与内层并列排序无关。
SELECT id, device, sec, qty, rolling,
       SUM(rolling) OVER w AS rolling_total
FROM (
    SELECT id, device, org, sec, qty,
           SUM(qty) OVER w AS rolling
    FROM events
    WINDOW w AS (PARTITION BY device ORDER BY sec
                 ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
) t
WINDOW w AS (PARTITION BY org ORDER BY sec
             RANGE BETWEEN 1 PRECEDING AND CURRENT ROW)
ORDER BY id DESC
