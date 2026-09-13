package dev.rag.knowledge;

import dev.rag.common.ApiException;

import java.time.*;
import java.util.Map;

/**
 * 外部解析器长任务截止时间（Deadline）追踪控制组件。
 * <p>
 * <b>可靠性设计：</b>
 * 将外部解析启动时间持久化至 checkpoint 中（externalTaskStartedAt），
 * 确保即使经历应用重启、轮询轮次切换或租约接管，也不会重置超时计时，防范长任务无限悬挂。
 */
final class ParserDeadline {
    static final String START = "externalTaskStartedAt";

    /**
     * 检查外部解析任务是否已超时。
     * <p>
     * <b>超时判定逻辑：</b>
     * 1. 验证 timeoutSeconds 处于 1~3600 秒合法区间；
     * 2. 若 checkpoint 中无 START 时间戳，则兼容旧任务提取创建时间，避免重启后超时时间被重新刷新；
     * 3. 计算已等待时长并与 timeoutSeconds 比对，超出则抛出 422 PARSER_TIMEOUT 异常。
     *
     * @param checkpoint     持久化的任务检查点 Map
     * @param jobCreatedAt   任务初始创建时间（Timestamp 或 LocalDateTime）
     * @param now            当前系统毫秒时间戳
     * @param timeoutSeconds 允许的最大超时秒数
     * @throws IllegalArgumentException 若超时参数不在 1~3600 秒之间
     * @throws ApiException             若任务运行总时间已超出上限（422 PARSER_TIMEOUT）
     */
    static void check(Map<String, Object> checkpoint, Object jobCreatedAt, long now, int timeoutSeconds) {
        if (timeoutSeconds < 1 || timeoutSeconds > 3600)
            throw new IllegalArgumentException("Parser deadline must be 1..3600 seconds");
        Object start = checkpoint.get(START);
        if (start == null) {
            // Older persisted tasks predate the explicit clock; never give them a fresh timeout on every poll.
            long created = jobCreatedAt instanceof java.sql.Timestamp t ? t.toInstant().toEpochMilli() : jobCreatedAt instanceof LocalDateTime t ? t.toInstant(ZoneOffset.UTC).toEpochMilli() : now;
            start = checkpoint.containsKey("externalTaskId") ? created : now;
            checkpoint.put(START, start);
        }
        long started = ((Number) start).longValue();
        if (now - started >= timeoutSeconds * 1000L)
            throw new ApiException(422, "PARSER_TIMEOUT", "解析任务超过总等待时限，请检查解析服务后重试");
    }

    private ParserDeadline() {
    }
}
