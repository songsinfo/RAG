package dev.rag.common;
import org.springframework.web.bind.annotation.*;
import org.springframework.http.ResponseEntity;
import java.util.Map;
@RestControllerAdvice
public class Errors {
 @ExceptionHandler(ApiException.class)
 ResponseEntity<?> api(ApiException e){return ResponseEntity.status(e.status).body(Map.of("requestId",Json.id(),"code",e.code,"message",e.getMessage(),"retryable",e.status==503||e.status==429));}
 @ExceptionHandler(Exception.class)
 ResponseEntity<?> unexpected(Exception e){return ResponseEntity.internalServerError().body(Map.of("requestId",Json.id(),"code","INTERNAL_ERROR","message","处理失败，请使用运行记录定位问题","retryable",false));}
}

