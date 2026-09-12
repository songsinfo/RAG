package dev.rag.identity;
import dev.rag.common.*;
import org.springframework.boot.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import java.util.*;

/** Explicit opt-in. Existing accounts are never reset by restarting. */
@Component
public class DemoSeed implements ApplicationRunner {
 private final Db db;private final TransactionTemplate tx;private final String password,environment;
 public DemoSeed(Db db,TransactionTemplate tx,@Value("${RAG_SEED_PASSWORD:}")String password,@Value("${rag.environment:demo}")String environment){this.db=db;this.tx=tx;this.password=password;this.environment=environment;}
 public void run(ApplicationArguments args){if(password.isBlank())return;if(!Set.of("demo","test","evaluation").contains(environment))throw new IllegalStateException("Seed requires isolated environment");if(password.length()<12)throw new IllegalStateException("Seed password requires at least 12 characters");String hash=new BCryptPasswordEncoder(12).encode(password);tx.executeWithoutResult(s->{for(String code:List.of("demo","test")){if(db.optional("SELECT id FROM tenant WHERE code=:c",Map.of("c",code)).isPresent())continue;String t=Json.id();db.insert("tenant",Db.params("id",t,"code",code,"name",code+" 示例组织"));for(String name:List.of("admin","ops","product","engineer")){String u=Json.id();db.insert("app_user",Db.params("tenant_id",t,"id",u,"login_name",name,"password_hash",hash,"is_admin",name.equals("admin")));if(!name.equals("admin")){String g=Json.id();db.insert("user_group",Db.params("tenant_id",t,"id",g,"name",name));db.insert("group_member",Db.params("tenant_id",t,"id",Json.id(),"group_id",g,"user_id",u));}}}});}
}
