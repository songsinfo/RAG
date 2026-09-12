package dev.rag.identity;
import dev.rag.common.*;
import jakarta.servlet.*;
import jakarta.servlet.http.*;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.util.*;
import java.util.function.Supplier;

/** Holds the tenant gate through actual response serialization, closing the final read race. */
@Component
public class AuthorityResponseFilter extends OncePerRequestFilter {
 private final AccessPolicy access;
 public AuthorityResponseFilter(AccessPolicy access){this.access=access;}
 protected boolean shouldNotFilter(HttpServletRequest r){String p=r.getRequestURI();return !p.startsWith("/api/v1/")||p.startsWith("/api/v1/auth/")||p.startsWith("/api/v1/internal/")||p.startsWith("/api/v1/health/");}
 protected void doFilterInternal(HttpServletRequest request,HttpServletResponse response,FilterChain chain)throws ServletException,IOException{try{Actor a=access.actor(request);Supplier<Void> process=()->{try{response.setHeader("Cache-Control","no-store");chain.doFilter(request,response);response.flushBuffer();return null;}catch(IOException e){throw new UncheckedIOException(e);}catch(ServletException e){throw new ServletFailure(e);}};if(Set.of("GET","HEAD","OPTIONS").contains(request.getMethod()))access.guard(a,List.of(),process);else access.write(a,process);}catch(ApiException e){if(response.isCommitted())throw e;response.setStatus(e.status);response.setContentType("application/json;charset=UTF-8");response.getWriter().write(Json.write(Map.of("code",e.code,"message",e.getMessage(),"requestId",Json.id(),"retryable",e.status>=500,"details",Map.of())));}catch(UncheckedIOException e){throw e.getCause();}catch(ServletFailure e){throw (ServletException)e.getCause();}}
 private static class ServletFailure extends RuntimeException{ServletFailure(ServletException e){super(e);}}
}
