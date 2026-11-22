-- 变种：只输出 id = 1 一行。过滤发生在两层窗口之外，
-- 未展示的行不改变前面层级已经定义好的窗口。
SELECT id, device, sec, qty, rolling, rolling_total
FROM (
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
) s
WHERE id = 1
ORDER BY id DESC
