-- SQLite
SELECT 
    -- Were the probability buckets stored correctly?
    AVG(p_strong_up + p_moderate_up) AS avg_p_up,
    AVG(p_strong_down + p_moderate_down) AS avg_p_down,
    -- Does direction match the buckets?
    SUM(CASE WHEN (p_strong_up + p_moderate_up) > (p_strong_down + p_moderate_down) 
                  AND predicted_direction = 'UP' THEN 1 ELSE 0 END) AS up_consistent,
    SUM(CASE WHEN (p_strong_up + p_moderate_up) > (p_strong_down + p_moderate_down) 
                  AND predicted_direction = 'DOWN' THEN 1 ELSE 0 END) AS up_inverted,
    SUM(CASE WHEN (p_strong_down + p_moderate_down) > (p_strong_up + p_moderate_up) 
                  AND predicted_direction = 'DOWN' THEN 1 ELSE 0 END) AS down_consistent,
    SUM(CASE WHEN (p_strong_down + p_moderate_down) > (p_strong_up + p_moderate_up) 
                  AND predicted_direction = 'UP' THEN 1 ELSE 0 END) AS down_inverted
FROM predictions
WHERE horizon_days = 5;