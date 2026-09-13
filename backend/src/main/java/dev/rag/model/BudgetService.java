package dev.rag.model;

import dev.rag.common.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.transaction.PlatformTransactionManager;

import java.math.*;
import java.time.*;
import java.util.*;

import static dev.rag.common.Db.params;

/**
 * 租户大模型调用预算与配额核心管理服务。
 * <p>
 * <b>关键设计与架构职责：</b>
 * <ul>
 *   <li><b>模型价格管理：</b>严格校验租户发布的 MODEL 配置，要求统一折算为 CNY 币种，并解析输入/输出每百万 Token 单价；</li>
 *   <li><b>Token 费用估算：</b>提供高精度的按量计费预估算法，统一按每百万 Token 费率折算，向上取整保留 6 位小数；</li>
 *   <li><b>批次任务配额刚性管控：</b>针对评估等后台批次作业，行级悲观锁控制作业累计消费金额（默认上限 10 元）及调用次数（默认 1000 次），杜绝批次任务失控刷量；</li>
 *   <li><b>事务性额度预扣与防超卖：</b>采用悲观锁（FOR UPDATE）锁定月度租户预算记录，实行“先预扣、后调用、再核销”机制；同时限制租户最大在途并发为 3，防范并发雪崩；</li>
 *   <li><b>多退少补最终核销：</b>支持调用成功（SETTLED）、未发起释放（NOT_SENT）与异常（UNKNOWN）流转，具备幂等防重入保障。</li>
 * </ul>
 */
@Service
public class BudgetService {
    private final Db db;
    private final TransactionTemplate tx;

    /**
     * 构造预算核心服务。
     *
     * @param db 统一数据库操作组件
     * @param manager 事务管理器，用于构建事务模板
     */
    public BudgetService(Db db, PlatformTransactionManager manager) {
        this.db = db;
        this.tx = new TransactionTemplate(manager);
    }

    /**
     * 获取当前租户最新生效的大模型计费单价模型配置。
     * <p>
     * 要求租户必须发布了状态为 PUBLISHED 的 MODEL 配置，币种必须为 CNY，
     * 且必须配置了有效的非负 inputPricePerMillion 与 outputPricePerMillion。
     *
     * @param a 操作主体
     * @return 包含计费单价与配置 ID 的配置 Map
     * @throws ApiException 若未发布模型配置或配置的价格字段缺失/币种不合规（409 MODEL_PRICING_NOT_CONFIGURED）
     */
    public Map<String, Object> pricing(Actor a) {
        var row = db.optional("SELECT * FROM config_revision WHERE tenant_id=:t AND kind='MODEL' AND status='PUBLISHED' ORDER BY created_at DESC LIMIT 1", params("t", a.tenantId())).orElseThrow(() -> new ApiException(409, "MODEL_PRICING_NOT_CONFIGURED", "请发布含 CNY 单价的模型配置"));
        var p = Json.object(row.get("payload").toString());
        if (!"CNY".equals(p.get("currency")))
            throw new ApiException(409, "MODEL_PRICING_NOT_CONFIGURED", "模型价格必须换算为 CNY");
        price(p, "inputPricePerMillion");
        price(p, "outputPricePerMillion");
        p.put("configId", row.get("id"));
        return p;
    }

    /**
     * 从配置字典中提取并校验非负十进制单价数值。
     *
     * @param p 配置字典 Map
     * @param key 价格字段名（如 inputPricePerMillion）
     * @return 校验后的 BigDecimal 单价值
     * @throws ApiException 若价格字段缺失或小于 0
     */
    private static BigDecimal price(Map<String, Object> p, String key) {
        try {
            var n = new BigDecimal(p.get(key).toString());
            if (n.signum() < 0) throw new NumberFormatException();
            return n;
        } catch (Exception e) {
            throw new ApiException(409, "MODEL_PRICING_NOT_CONFIGURED", "缺少明确模型单价或上界");
        }
    }

    /**
     * 估算大模型调用的费用（元/CNY）。
     * <p>
     * 计算公式：
     * <pre>
     * cost = (inputTokens * inputPricePerMillion + outputTokens * outputPricePerMillion) / 1,000,000
     * </pre>
     * 结果保留 6 位小数，使用 {@link RoundingMode#CEILING}（向上进位）以确保预算覆盖上限。
     *
     * @param p 计费模型价格配置 Map
     * @param input 预估输入 Token 数量
     * @param output 预估输出 Token 数量
     * @return 估算费用 BigDecimal（CNY）
     */
    public static BigDecimal estimate(Map<String, Object> p, long input, long output) {
        return price(p, "inputPricePerMillion").multiply(BigDecimal.valueOf(input)).add(price(p, "outputPricePerMillion").multiply(BigDecimal.valueOf(output))).divide(BigDecimal.valueOf(1_000_000), 6, RoundingMode.CEILING);
    }

    /**
     * 校验指定的后台批次任务作业当前处于运行中且未被取消。
     *
     * @param actor 操作主体
     * @param jobId 目标批次作业 ID
     * @throws ApiException 若作业状态非 RUNNING 或已标记 cancel_requested_at（409 CANCELLED）
     */
    public void requireBatchRunning(Actor actor, String jobId) {
        var job = db.one("SELECT status,cancel_requested_at FROM job WHERE tenant_id=:t AND id=:id", params("t", actor.tenantId(), "id", jobId));
        if (!"RUNNING".equals(job.get("status")) || job.get("cancel_requested_at") != null)
            throw new ApiException(409, "CANCELLED", "所属后台批次已停止");
    }

    /**
     * 在事务中校验批次预算限额并原子登记一次模型调用（model_call）。
     * <p>
     * <b>配额与并发保障逻辑：</b>
     * <ol>
     *   <li>行级悲观锁锁定 job 记录，校验批次当前未被取消；</li>
     *   <li>从 checkpoint 中提取当前批次的预算硬顶（如 maxCostCny 默认 10 元，maxModelCalls 默认 1000 次）；</li>
     *   <li>统计该批次在 {@code model_call} 中已产生的所有调用次数以及消耗金额（已结算金额或预扣金额）；</li>
     *   <li>累加本次预扣金额，若调用次数超限或累计费用超过限额，立即抛出 429 BUDGET_EXCEEDED 熔断；</li>
     *   <li>校验通过后将本次调用记录插入 {@code model_call} 表。</li>
     * </ol>
     *
     * @param actor 操作主体
     * @param jobId 所属后台批次作业 ID
     * @param call 待持久化的模型调用流水参数 Map
     * @throws ApiException 若批次已停止或预算/调用次数超限
     */
    public void recordBatchCall(Actor actor, String jobId, Map<String, Object> call) {
        tx.executeWithoutResult(s -> {
            var job = db.one("SELECT * FROM job WHERE tenant_id=:t AND id=:id FOR UPDATE", params("t", actor.tenantId(), "id", jobId));
            if (!"RUNNING".equals(job.get("status")) || job.get("cancel_requested_at") != null)
                throw new ApiException(409, "CANCELLED", "所属后台批次已停止");
            var checkpoint = job.get("checkpoint") == null ? Map.<String, Object>of() : Json.object(job.get("checkpoint").toString());
            BigDecimal limit = new BigDecimal(checkpoint.getOrDefault("maxCostCny", checkpoint.getOrDefault("maxCost", "10")).toString());
            int maxCalls = Json.integer(checkpoint, "maxModelCalls", 1000);
            if (limit.signum() <= 0 || limit.compareTo(BigDecimal.TEN) > 0 || maxCalls < 1)
                throw new ApiException(429, "BUDGET_EXCEEDED", "后台批量预算上限无效");
            // Link every turn before its worker starts; eval_result.qa_run_id only retains the latest turn.
            var usage = db.one("SELECT COUNT(*) AS calls,COALESCE(SUM(COALESCE(settled_amount,reserved_amount)),0) AS amount FROM model_call WHERE tenant_id=:t AND (job_id=:j OR run_id IN (SELECT id FROM qa_run WHERE tenant_id=:t AND JSON_UNQUOTE(JSON_EXTRACT(budget_json,'$.evaluationJobId'))=:j))", params("t", actor.tenantId(), "j", jobId));
            BigDecimal next = new BigDecimal(usage.get("amount").toString()).add(new BigDecimal(call.get("reserved_amount").toString()));
            if (((Number) usage.get("calls")).intValue() >= maxCalls || next.compareTo(limit) > 0)
                throw new ApiException(429, "BUDGET_EXCEEDED", "后台批次调用或费用上限已达到");
            db.insert("model_call", call);
        });
    }

    /**
     * 事务性预扣租户月度模型调用额度（防超卖与限流）。
     * <p>
     * <b>控制步骤与并发防护：</b>
     * <ol>
     *   <li>入参校验：预扣金额必须为非负数；</li>
     *   <li>幂等性重入检测：若已有相同 caller_environment 和 caller_call_id 的预扣记录，
     *       比对金额是否一致（一致直接返回旧记录，不一致抛 409 IDEMPOTENCY_CONFLICT）；</li>
     *   <li>悲观行锁锁定月度预算：若当月记录不存在则 INSERT IGNORE 初始化（月限额 100 元 CNY），通过 {@code SELECT ... FOR UPDATE} 加锁；</li>
     *   <li>加锁后二次幂等检测：防止并发新建时的间隙冲突；</li>
     *   <li>在途并发限流：查询未过期的 RESERVED 状态流水数，若达到 3 则抛出 429 RATE_LIMITED 限流；</li>
     *   <li>总额度校验：已预扣额 + 已结算额 + 本次申请额 必须小于等于月度限额，否则抛出 429 BUDGET_RESERVATION_DENIED；</li>
     *   <li>扣减与落盘：累加 tenant_budget.reserved_amount，插入状态为 RESERVED 的流水记录（有效期 1 小时）。</li>
     * </ol>
     *
     * @param actor 操作主体
     * @param environment 发起调用的客户端环境（如 evaluation, demo 等）
     * @param callId 外部调用流水唯一 ID
     * @param amount 申请预扣的最大金额（CNY）
     * @return 预扣流水记录 Map（包含 id, status, reserved_amount 等）
     * @throws ApiException 400（参数非法）、409（幂等冲突）、429（在途超限或预算不足）
     */
    public Map<String, Object> reserve(Actor actor, String environment, String callId, BigDecimal amount) {
        if (amount.signum() < 0) throw new ApiException(400, "INVALID_INPUT", "预算不能为负");
        return tx.execute(s -> {
            var existing = db.optional("SELECT * FROM budget_reservation WHERE tenant_id=:t AND caller_environment=:e AND caller_call_id=:c", params("t", actor.tenantId(), "e", environment, "c", callId));
            if (existing.isPresent()) {
                if (new BigDecimal(existing.get().get("reserved_amount").toString()).compareTo(amount) != 0)
                    throw new ApiException(409, "IDEMPOTENCY_CONFLICT", "预留参数发生变化");
                return existing.get();
            }
            String month = YearMonth.now(ZoneOffset.UTC).toString();
            db.exec("INSERT IGNORE INTO tenant_budget(tenant_id,id,period_month,currency,limit_amount) VALUES(:t,:id,:m,'CNY',100)", params("t", actor.tenantId(), "id", Json.id(), "m", month));
            var budget = db.one("SELECT * FROM tenant_budget WHERE tenant_id=:t AND period_month=:m FOR UPDATE", params("t", actor.tenantId(), "m", month));
            var repeated = db.optional("SELECT * FROM budget_reservation WHERE tenant_id=:t AND caller_environment=:e AND caller_call_id=:c", params("t", actor.tenantId(), "e", environment, "c", callId));
            if (repeated.isPresent()) {
                if (new BigDecimal(repeated.get().get("reserved_amount").toString()).compareTo(amount) != 0)
                    throw new ApiException(409, "IDEMPOTENCY_CONFLICT", "预留参数发生变化");
                return repeated.get();
            }
            int inFlight = ((Number) db.one("SELECT COUNT(*) AS n FROM budget_reservation WHERE tenant_id=:t AND status='RESERVED' AND expires_at>CURRENT_TIMESTAMP", params("t", actor.tenantId())).get("n")).intValue();
            if (inFlight >= 3) throw new ApiException(429, "RATE_LIMITED", "共用模型在途并发已满");
            var limit = new BigDecimal(budget.get("limit_amount").toString());
            var total = new BigDecimal(budget.get("reserved_amount").toString()).add(new BigDecimal(budget.get("settled_amount").toString())).add(amount);
            if (total.compareTo(limit) > 0) throw new ApiException(429, "BUDGET_RESERVATION_DENIED", "月度预算不足");
            String id = Json.id();
            db.exec("UPDATE tenant_budget SET reserved_amount=reserved_amount+:a WHERE tenant_id=:t AND id=:id", params("t", actor.tenantId(), "id", budget.get("id"), "a", amount));
            var row = params("tenant_id", actor.tenantId(), "id", id, "budget_id", budget.get("id"), "caller_environment", environment, "caller_call_id", callId, "status", "RESERVED", "reserved_amount", amount, "expires_at", java.sql.Timestamp.from(Instant.now().plusSeconds(3600)));
            db.insert("budget_reservation", row);
            return row;
        });
    }

    /**
     * 事务性结算或释放模型调用预算（核销多退少补）。
     * <p>
     * <b>状态机与账务流转规则：</b>
     * <ul>
     *   <li>合法目标状态：SETTLED（按量扣费）、NOT_SENT（请求未发释放全款）、UNKNOWN（挂起未决）；</li>
     *   <li>行级悲观锁：锁定 budget_reservation 行记录；</li>
     *   <li>幂等终态保护：若记录已处于 SETTLED 或 NOT_SENT，比对结算金额是否一致，防止重复核销引发账目错乱；</li>
     *   <li>对于 UNKNOWN：仅标记状态，保留预扣额直至后续补偿；</li>
     *   <li>对于 SETTLED / NOT_SENT：原子更新 tenant_budget 表，减去原有预扣额（reserved_amount），
     *       加上实际结算额（settled_amount，NOT_SENT 时为 0），并更新流水行状态与实扣金额。</li>
     * </ul>
     *
     * @param actor 操作主体
     * @param id 预扣流水唯一 ID
     * @param status 目标结算状态（SETTLED / NOT_SENT / UNKNOWN）
     * @param actual 实际应扣金额（SETTLED 时不可为 null 且非负；NOT_SENT 时置为 0）
     * @throws ApiException 400（参数非法）、409（幂等冲突）
     */
    public void settle(Actor actor, String id, String status, BigDecimal actual) {
        if (!Set.of("SETTLED", "NOT_SENT", "UNKNOWN").contains(status) || actual != null && actual.signum() < 0)
            throw new ApiException(400, "INVALID_INPUT", "结算参数无效");
        tx.executeWithoutResult(s -> {
            var r = db.one("SELECT * FROM budget_reservation WHERE tenant_id=:t AND id=:id FOR UPDATE", params("t", actor.tenantId(), "id", id));
            if (Set.of("SETTLED", "NOT_SENT").contains(r.get("status").toString())) {
                if (!status.equals(r.get("status")) || actual != null && new BigDecimal(r.get("settled_amount").toString()).compareTo(actual) != 0)
                    throw new ApiException(409, "IDEMPOTENCY_CONFLICT", "结算已固定");
                return;
            }
            if (status.equals("UNKNOWN")) {
                db.exec("UPDATE budget_reservation SET status='UNKNOWN' WHERE tenant_id=:t AND id=:id", params("t", actor.tenantId(), "id", id));
                return;
            }
            BigDecimal amount = status.equals("NOT_SENT") ? BigDecimal.ZERO : Objects.requireNonNull(actual);
            db.exec("UPDATE tenant_budget SET reserved_amount=reserved_amount-:r,settled_amount=settled_amount+:a WHERE tenant_id=:t AND id=:id", params("r", r.get("reserved_amount"), "a", amount, "t", actor.tenantId(), "id", r.get("budget_id")));
            db.exec("UPDATE budget_reservation SET status=:s,settled_amount=:a WHERE tenant_id=:t AND id=:id", params("s", status, "a", amount, "t", actor.tenantId(), "id", id));
        });
    }

    /**
     * 查询租户最近 12 个月的月度预算分配与消耗汇总列表。
     *
     * @param a 操作主体
     * @return 按月份倒序排列的预算统计列表（包含 periodMonth, currency, limitAmount, reservedAmount, settledAmount）
     */
    public List<Map<String, Object>> summary(Actor a) {
        return db.list("SELECT period_month AS periodMonth,currency,limit_amount AS limitAmount,reserved_amount AS reservedAmount,settled_amount AS settledAmount FROM tenant_budget WHERE tenant_id=:t ORDER BY period_month DESC LIMIT 12", params("t", a.tenantId()));
    }
}
