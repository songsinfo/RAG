package dev.rag.common;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import java.util.*;
@Component
public class Db {
 public final NamedParameterJdbcTemplate jdbc;
 public Db(NamedParameterJdbcTemplate jdbc){this.jdbc=jdbc;}
 public List<Map<String,Object>> list(String sql,Map<String,?> params){return jdbc.queryForList(sql,params);}
 public Map<String,Object> one(String sql,Map<String,?> params){var rows=list(sql,params);if(rows.isEmpty())throw ApiException.missing();return rows.getFirst();}
 public Optional<Map<String,Object>> optional(String sql,Map<String,?> params){var rows=list(sql,params);return rows.isEmpty()?Optional.empty():Optional.of(rows.getFirst());}
 public int exec(String sql,Map<String,?> params){return jdbc.update(sql,params);}
 public void insert(String table,Map<String,?> values){
  if(!table.matches("[a-z0-9_]+")||values.keySet().stream().anyMatch(k->!k.matches("[a-z0-9_]+")))throw new IllegalArgumentException("Unsafe SQL identifier");
  jdbc.update("INSERT INTO "+table+" ("+String.join(",",values.keySet())+") VALUES ("+String.join(",",values.keySet().stream().map(k->":"+k).toList())+")",values);
 }
 public static Map<String,Object> params(Object...pairs){Map<String,Object> p=new LinkedHashMap<>();for(int i=0;i<pairs.length;i+=2)p.put((String)pairs[i],pairs[i+1]);return p;}
}

