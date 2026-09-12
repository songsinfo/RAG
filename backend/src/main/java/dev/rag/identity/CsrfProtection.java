package dev.rag.identity;
import dev.rag.common.*;
import jakarta.servlet.http.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.*;
import org.springframework.web.servlet.config.annotation.*;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.net.URI;
import java.util.*;

@Component
public class CsrfProtection implements HandlerInterceptor,WebMvcConfigurer {
 private final byte[] key;private final boolean secure;
 public CsrfProtection(@Value("${rag.csrf-secret:}")String secret,@Value("${rag.cookie-secure:false}")boolean secure){this.key=(secret.isBlank()?IdentityController.token():secret).getBytes(StandardCharsets.UTF_8);this.secure=secure;}
 @Override public void addInterceptors(InterceptorRegistry registry){registry.addInterceptor(this).addPathPatterns("/api/v1/**").excludePathPatterns("/api/v1/internal/**");}
 private String sign(String data){try{Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(key,"HmacSHA256"));return Base64.getUrlEncoder().withoutPadding().encodeToString(mac.doFinal(data.getBytes(StandardCharsets.UTF_8)));}catch(Exception e){throw new IllegalStateException(e);}}
 private String binding(HttpServletRequest r){String s=AccessPolicy.cookie(r,"RAG_SESSION");return s.isBlank()?AccessPolicy.cookie(r,"RAG_NONCE"):s;}
 public String issue(HttpServletRequest r,HttpServletResponse response){String b=binding(r);if(b.isBlank()){b=IdentityController.token();IdentityController.cookie(response,"RAG_NONCE",b,true,secure,3600);}String raw=(System.currentTimeMillis()+3600000)+"."+IdentityController.token();String token=raw+"."+sign(b+":"+raw);IdentityController.cookie(response,"RAG_CSRF",token,false,secure,3600);return token;}
 @Override public boolean preHandle(HttpServletRequest r,HttpServletResponse response,Object handler){if(Set.of("GET","HEAD","OPTIONS").contains(r.getMethod()))return true;String origin=r.getHeader("Origin");if(origin!=null){try{URI u=URI.create(origin);int port=u.getPort()<0?(u.getScheme().equals("https")?443:80):u.getPort();if(!Objects.equals(u.getScheme(),r.getScheme())||!Objects.equals(u.getHost(),r.getServerName())||port!=r.getServerPort())throw denied();}catch(IllegalArgumentException e){throw denied();}}String header=r.getHeader("X-CSRF-Token"),cookie=AccessPolicy.cookie(r,"RAG_CSRF");if(header==null||!MessageDigest.isEqual(header.getBytes(StandardCharsets.UTF_8),cookie.getBytes(StandardCharsets.UTF_8)))throw denied();String[] p=header.split("\\.");if(p.length!=3)throw denied();try{if(Long.parseLong(p[0])<System.currentTimeMillis()||!MessageDigest.isEqual(p[2].getBytes(StandardCharsets.UTF_8),sign(binding(r)+":"+p[0]+"."+p[1]).getBytes(StandardCharsets.UTF_8)))throw denied();}catch(NumberFormatException e){throw denied();}return true;}
 private ApiException denied(){return new ApiException(403,"CSRF_INVALID","请刷新安全凭据后重试");}
}
