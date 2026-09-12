package dev.rag.identity;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.*;
import jakarta.servlet.http.Cookie;
import dev.rag.common.ApiException;
import static org.junit.jupiter.api.Assertions.*;
class CsrfProtectionTest {
 @Test void requiresSignedTokenBoundToSessionAndSameOrigin(){var csrf=new CsrfProtection("test-secret",false);var get=new MockHttpServletRequest();get.setCookies(new Cookie("RAG_SESSION","session-a"));String token=csrf.issue(get,new MockHttpServletResponse());var post=new MockHttpServletRequest("POST","/api/v1/admin/documents");post.setCookies(new Cookie("RAG_SESSION","session-a"),new Cookie("RAG_CSRF",token));post.addHeader("X-CSRF-Token",token);assertTrue(csrf.preHandle(post,new MockHttpServletResponse(),new Object()));post.setCookies(new Cookie("RAG_SESSION","session-b"),new Cookie("RAG_CSRF",token));assertThrows(ApiException.class,()->csrf.preHandle(post,new MockHttpServletResponse(),new Object()));post.setCookies(new Cookie("RAG_SESSION","session-a"),new Cookie("RAG_CSRF",token));post.addHeader("Origin","https://attacker.invalid");assertThrows(ApiException.class,()->csrf.preHandle(post,new MockHttpServletResponse(),new Object()));}
}
