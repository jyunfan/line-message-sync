// Intercept sqlite3_key and sqlite3_key_v2 to extract the SQLCipher key
// that Line uses to open its encrypted .edb database.

var targets = ["sqlite3_key", "sqlite3_key_v2"];

targets.forEach(function(name) {
    var sym = Module.findExportByName(null, name);
    if (!sym) {
        console.log("[!] " + name + " not found");
        return;
    }
    console.log("[+] Hooking " + name + " at " + sym);

    Interceptor.attach(sym, {
        onEnter: function(args) {
            // sqlite3_key(db, pKey, nKey)
            // sqlite3_key_v2(db, zDbName, pKey, nKey)
            var isV2 = (name === "sqlite3_key_v2");
            var keyPtr = isV2 ? args[2] : args[1];
            var keyLen = isV2 ? args[3].toInt32() : args[2].toInt32();

            if (keyLen <= 0 || keyLen > 256) {
                console.log("[!] " + name + " called with suspicious keyLen=" + keyLen);
                return;
            }

            var keyBytes = keyPtr.readByteArray(keyLen);
            var keyHex = Array.from(new Uint8Array(keyBytes))
                .map(b => b.toString(16).padStart(2, "0"))
                .join("");
            var keyStr = "";
            try { keyStr = keyPtr.readUtf8String(keyLen); } catch(e) { keyStr = "<binary>"; }

            console.log("\n[KEY FOUND] " + name);
            console.log("  hex: " + keyHex);
            console.log("  str: " + keyStr);
            console.log("  len: " + keyLen);
        }
    });
});

console.log("[*] Hooks installed, waiting for Line to open its database...");
