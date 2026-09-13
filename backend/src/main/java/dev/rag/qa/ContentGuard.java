package dev.rag.qa;

import dev.rag.common.ApiException;
import org.springframework.stereotype.Component;

import java.util.regex.Pattern;

/**
 * 敏感内容与提示词注入（Prompt Injection）双向安全防护组件。
 * <p>
 * <b>核心防护能力：</b>
 * <ul>
 *   <li><b>提示词注入（Prompt Injection）拦截：</b>
 *       检测中英文常见的越权攻击模式（如“忽略之前指令”、“输出系统提示词”等）；</li>
 *   <li><b>高危凭证阻断（Secret Detection）：</b>
 *       正则匹配私钥头（RSA/EC/OPENSSH）、Bearer Token 及 key/token/secret/password 等明文凭据；</li>
 *   <li><b>个人隐私信息保护（PII Protection）：</b>
 *       针对中国大陆手机号（11 位）、居民身份证号（18 位）进行高置信度前置过滤与输出阻断。</li>
 * </ul>
 */
@Component
public final class ContentGuard {
    private static final Pattern PHONE = Pattern.compile("(?<!\\d)1[3-9]\\d{9}(?!\\d)");
    private static final Pattern ID_CARD = Pattern.compile("(?<!\\d)\\d{17}[0-9Xx](?!\\d)");
    private static final Pattern PRIVATE_KEY = Pattern.compile("-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----");
    private static final Pattern BEARER = Pattern.compile("(?i)\\bBearer\\s+[A-Za-z0-9._~+/-]{16,}=*");
    private static final Pattern CREDENTIAL = Pattern.compile("(?i)(?:api[_-]?key|access[_-]?token|password|secret)\\s*[:=：]\\s*[^\\s,;，；]{8,}");
    private static final Pattern INJECTION = Pattern.compile("(?iu)^\\s*(?:please\\s+)?(?:ignore\\s+(?:all\\s+)?(?:previous|prior|system)\\s+instructions?|reveal\\s+(?:the\\s+)?system\\s+prompt|(?:请)?忽略.{0,12}(?:系统|之前|以上).{0,8}(?:指令|提示)|(?:请)?(?:泄露|显示|输出).{0,8}(?:系统提示词|system\\s+prompt))");

    /**
     * 校验用户输入文本的安全性。
     *
     * @param text 用户输入的提问或追问文本
     * @throws ApiException 若包含提示注入（400 PROMPT_INJECTION_BLOCKED）、密钥凭据（400 SECRET_INPUT_BLOCKED）或个人隐私（400 PII_INPUT_BLOCKED）
     */
    public void validateInput(String text) {
        if (INJECTION.matcher(text).find())
            throw new ApiException(400, "PROMPT_INJECTION_BLOCKED", "请求包含试图修改系统指令的内容");
        if (containsSecret(text)) throw new ApiException(400, "SECRET_INPUT_BLOCKED", "请求包含疑似密钥或凭据");
        if (containsPii(text)) throw new ApiException(400, "PII_INPUT_BLOCKED", "请求包含高置信度个人敏感信息");
    }

    /**
     * 校验模型输出回答的合规性与安全性，防止模型幻觉或数据逆向导致敏感凭据/PII 泄漏。
     *
     * @param text 准备下发给客户端的模型回答文本
     * @throws ApiException 若检测出敏感凭证或个人隐私信息（422 SENSITIVE_OUTPUT_BLOCKED）
     */
    public void validateOutput(String text) {
        if (containsSecret(text) || containsPii(text))
            throw new ApiException(422, "SENSITIVE_OUTPUT_BLOCKED", "回答包含高置信度敏感信息");
    }

    /**
     * 检测文本中是否包含高危密钥或认证凭据。
     *
     * @param text 待检测文本
     * @return true 若匹配私钥头、Bearer 令牌或密码/Token 键值对
     */
    private boolean containsSecret(String text) {
        return PRIVATE_KEY.matcher(text).find() || BEARER.matcher(text).find() || CREDENTIAL.matcher(text).find();
    }

    /**
     * 检测文本中是否包含个人隐私敏感信息（PII）。
     *
     * @param text 待检测文本
     * @return true 若匹配 11 位手机号或 18 位身份证号
     */
    private boolean containsPii(String text) {
        return PHONE.matcher(text).find() || ID_CARD.matcher(text).find();
    }
}
