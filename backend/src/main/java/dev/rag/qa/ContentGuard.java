package dev.rag.qa;

import dev.rag.common.ApiException;
import org.springframework.stereotype.Component;
import java.util.regex.Pattern;

@Component
public final class ContentGuard {
 private static final Pattern PHONE=Pattern.compile("(?<!\\d)1[3-9]\\d{9}(?!\\d)");
 private static final Pattern ID_CARD=Pattern.compile("(?<!\\d)\\d{17}[0-9Xx](?!\\d)");
 private static final Pattern PRIVATE_KEY=Pattern.compile("-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----");
 private static final Pattern BEARER=Pattern.compile("(?i)\\bBearer\\s+[A-Za-z0-9._~+/-]{16,}=*");
 private static final Pattern CREDENTIAL=Pattern.compile("(?i)(?:api[_-]?key|access[_-]?token|password|secret)\\s*[:=：]\\s*[^\\s,;，；]{8,}");
 private static final Pattern INJECTION=Pattern.compile("(?iu)^\\s*(?:please\\s+)?(?:ignore\\s+(?:all\\s+)?(?:previous|prior|system)\\s+instructions?|reveal\\s+(?:the\\s+)?system\\s+prompt|(?:请)?忽略.{0,12}(?:系统|之前|以上).{0,8}(?:指令|提示)|(?:请)?(?:泄露|显示|输出).{0,8}(?:系统提示词|system\\s+prompt))");

 public void validateInput(String text){
  if(INJECTION.matcher(text).find())throw new ApiException(400,"PROMPT_INJECTION_BLOCKED","请求包含试图修改系统指令的内容");
  if(containsSecret(text))throw new ApiException(400,"SECRET_INPUT_BLOCKED","请求包含疑似密钥或凭据");
  if(containsPii(text))throw new ApiException(400,"PII_INPUT_BLOCKED","请求包含高置信度个人敏感信息");
 }

 public void validateOutput(String text){
  if(containsSecret(text)||containsPii(text))throw new ApiException(422,"SENSITIVE_OUTPUT_BLOCKED","回答包含高置信度敏感信息");
 }

 private boolean containsSecret(String text){return PRIVATE_KEY.matcher(text).find()||BEARER.matcher(text).find()||CREDENTIAL.matcher(text).find();}
 private boolean containsPii(String text){return PHONE.matcher(text).find()||ID_CARD.matcher(text).find();}
}
