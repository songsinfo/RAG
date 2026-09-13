package dev.rag.model;

import dev.rag.common.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.math.BigDecimal;
import java.util.*;

import static dev.rag.common.Db.params;

/**
 * 预算授权与配额管理门面组件。
 * <p>
 * 支持本地单机记账与跨环境集中式远程预算认证（Remote Budget Authority）两种模式：
 * <ul>
 *   <li>当未配置 {@code rag.budget-authority-url} 时，降级为单机模式，委派给本地 {@link BudgetService} 执行事务性额度预扣与结算；</li>
 *   <li>当配置了远程服务 URL 时，通过 HTTP 协议与远程预算中心通信，支持分布式多实例环境下的总配额管控；</li>
 *   <li><b>隐私保障原则：</b>发往远程预算中心的仅为 Token 数量、环境名与调用标识等元数据，绝不包含用户 Prompt 或文档原文。</li>
 * </ul>
 */
@Component
public class BudgetAuthority {
    /**
     * 预算预扣凭证快照。
     *
     * @param id 预扣流水唯一 ID
     * @param authority 授权方标识（环境名或远程服务 URL）
     * @param amount 预扣金额预估上限
     * @param price 预扣时刻生效的价格模型快照（包含 perInputM / perOutputM 费率）
     * @param signature 远程认证中心的价格签名（防篡改凭证），本地模式下为空
     */
    public record Reservation(String id, String authority, BigDecimal amount, Map<String, Object> price, String signature) {
    }

    private final BudgetService local;
    private final String url, token, environment;
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

    /**
     * 构造预算授权门面组件。
     *
     * @param local 本地预算服务组件
     * @param url 远程预算认证中心基础地址（可为空）
     * @param token 内部预算服务调用凭据 Bearer Token
     * @param environment 当前应用运行环境标识（如 demo, staging, prod）
     */
    public BudgetAuthority(BudgetService local, @Value("${rag.budget-authority-url:}") String url, @Value("${rag.internal-budget-token:}") String token, @Value("${rag.environment:demo}") String environment) {
        this.local = local;
        this.url = url.replaceAll("/$", "");
        this.token = token;
        this.environment = environment;
    }

    /**
     * 为即将发起的大模型调用预扣费用额度。
     * <p>
     * <b>处理逻辑：</b>
     * <ul>
     *   <li>本地模式：使用传入的本地价格快照计算最坏情况消费额上限，写入数据库预扣记录；</li>
     *   <li>远程模式：向远程认证中心申请额度预扣，获得防篡改的价格签名凭据与预扣流水 ID；若额度不足远程返回 429。</li>
     * </ul>
     *
     * @param a 操作主体
     * @param callId 外部调用流水 ID
     * @param input 最大可能输入的 Token 数量预估
     * @param output 最大允许生成的 Output Token 限制
     * @param localPrice 本地配置的模型定价信息
     * @return 预扣凭证对象 {@link Reservation}
     * @throws ApiException 若预算不足（429 BUDGET_RESERVATION_DENIED）或远程不可达（503）
     */
    @SuppressWarnings("unchecked")
    public Reservation reserve(Actor a, String callId, long input, int output, Map<String, Object> localPrice) {
        if (url.isBlank()) {
            var amount = BudgetService.estimate(localPrice, input, output);
            var row = local.reserve(a, environment, callId, amount);
            return new Reservation(row.get("id").toString(), environment, amount, localPrice, "");
        }
        var r = post("/reservations", params("callerEnvironment", environment, "callId", callId, "maxInputTokens", input, "maxOutputTokens", output));
        return new Reservation(Json.str(r, "reservationId"), url, new BigDecimal(r.get("reservedAmount").toString()), (Map<String, Object>) r.get("pricingSnapshot"), Json.str(r, "pricingSignature"));
    }

    /**
     * 大模型调用完成后执行额度实际结算与多退少补。
     *
     * @param a 操作主体
     * @param r 此前预扣成功的 {@link Reservation} 凭证
     * @param status 结算状态：SETTLED（正常按量扣费）、NOT_SENT（请求未发出退回全款）、FAILED（调用失败取消）
     * @param input 实际消耗的输入 Token 数（结算时使用）
     * @param output 实际生成的输出 Token 数（结算时使用）
     */
    public void settle(Actor a, Reservation r, String status, Long input, Long output) {
        if (url.isBlank()) {
            local.settle(a, r.id(), status, status.equals("SETTLED") ? BudgetService.estimate(r.price(), input, output) : status.equals("NOT_SENT") ? BigDecimal.ZERO : null);
            return;
        }
        post("/reservations/" + r.id() + "/settle", params("status", status, "inputTokens", input, "outputTokens", output, "pricingSnapshot", r.price(), "pricingSignature", r.signature()));
    }

    /**
     * 向远程预算认证中心发起内部安全 HTTP POST 请求。
     *
     * @param path API 相对子路径（如 "/reservations"）
     * @param body 请求负载参数
     * @return 远程服务端响应的 JSON Map
     * @throws ApiException 429 额度耗尽、503 服务不可用或调用中断
     */
    private Map<String, Object> post(String path, Map<String, Object> body) {
        if (token.isBlank()) throw new ApiException(503, "BUDGET_AUTHORITY_UNAVAILABLE", "远程预算认证未配置");
        try {
            var request = HttpRequest.newBuilder(URI.create(url + "/api/v1/internal/budget" + path)).timeout(Duration.ofSeconds(10)).header("Authorization", "Bearer " + token).header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(Json.write(body))).build();
            var r = http.send(request, HttpResponse.BodyHandlers.ofString());
            if (r.statusCode() == 429) throw new ApiException(429, "BUDGET_RESERVATION_DENIED", "共用预算已耗尽");
            if (r.statusCode() != 200)
                throw new ApiException(503, "BUDGET_AUTHORITY_UNAVAILABLE", "共用预算服务拒绝调用");
            return Json.object(r.body());
        } catch (ApiException e) {
            throw e;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ApiException(503, "BUDGET_AUTHORITY_UNAVAILABLE", "预算调用中断");
        } catch (Exception e) {
            throw new ApiException(503, "BUDGET_AUTHORITY_UNAVAILABLE", "共用预算服务不可用");
        }
    }
}
