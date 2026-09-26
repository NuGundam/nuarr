// Sonarr / Radarr "Import Using Script" -> nuarr chooses the disk.
//
// The same contract as arr_import.py, compiled, because the arrs start this
// once per file and a Python interpreter took about a second to start on this
// box - longer than copying a 200 MB episode. This starts in a few tens of ms.
//
//   [ExtraFile]<path>          sidecars next to the source, so extras still import
//   [MoveStatus]MoveComplete   nuarr put the file at the destination
//   [MoveStatus]DeferMove      nuarr stepped aside - the arr imports it itself
//
// Always exits 0: a non-zero exit fails the import, and nothing here is worth that.
using System;
using System.IO;
using System.Net;
using System.Text;

class ArrImport
{
    static string Env(string name)
    {
        foreach (var p in new[] { "Sonarr_", "Radarr_" })
        {
            var v = Environment.GetEnvironmentVariable(p + name);
            if (!string.IsNullOrEmpty(v)) return v;
        }
        return "";
    }

    static string J(string s)
    {
        var b = new StringBuilder("\"");
        foreach (var c in s ?? "")
        {
            if (c == '"' || c == '\\') { b.Append('\\').Append(c); }
            else if (c < 0x20) { b.Append("\\u").Append(((int)c).ToString("x4")); }
            else b.Append(c);
        }
        return b.Append('"').ToString();
    }

    static int Main(string[] args)
    {
        string status = "DeferMove";
        try
        {
            if (Env("EventType").Equals("Test", StringComparison.OrdinalIgnoreCase)) return 0;
            string src = args.Length > 0 ? args[0] : Env("SourcePath");
            string dst = args.Length > 1 ? args[1] : Env("DestinationPath");
            string arr = !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("Sonarr_SourcePath"))
                         ? "Sonarr" : "Radarr";
            try
            {
                string dir = Path.GetDirectoryName(src);
                string stem = Path.GetFileNameWithoutExtension(src).ToLowerInvariant();
                var ext = new[] { ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".nfo" };
                foreach (var f in Directory.GetFiles(dir))
                {
                    var n = Path.GetFileName(f).ToLowerInvariant();
                    if (n.StartsWith(stem) && Array.IndexOf(ext, Path.GetExtension(n)) >= 0)
                        Console.WriteLine("[ExtraFile]" + f);
                }
            }
            catch { }
            string body = "{\"src\":" + J(src) + ",\"dst\":" + J(dst) + ",\"mode\":" + J(Env("TransferMode"))
                        + ",\"arr\":" + J(arr) + "}";
            // nuarr's own import listener first (no web server in the way),
            // the web route if that port does not answer.
            string text = null;
            foreach (var url in new[] { "http://127.0.0.1:8771/import",
                                        "http://127.0.0.1:8770/api/arrimport" })
            {
                try
                {
                    var req = (HttpWebRequest)WebRequest.Create(url);
                    req.Method = "POST";
                    req.ContentType = "application/json";
                    req.Timeout = req.ReadWriteTimeout = 4 * 3600 * 1000;
                    req.Proxy = null;               // no proxy auto-detect: that alone costs seconds
                    var bytes = Encoding.UTF8.GetBytes(body);
                    using (var s = req.GetRequestStream()) s.Write(bytes, 0, bytes.Length);
                    using (var resp = (HttpWebResponse)req.GetResponse())
                    using (var rd = new StreamReader(resp.GetResponseStream()))
                        text = rd.ReadToEnd().Replace(" ", "");
                    break;
                }
                catch (WebException e)
                {
                    if (e.Status != WebExceptionStatus.ConnectFailure) throw;
                }
            }
            if (text != null && text.Contains("\"ok\":true") && File.Exists(dst)) status = "MoveComplete";
        }
        catch { status = "DeferMove"; }
        Console.WriteLine("[MoveStatus]" + status);
        return 0;
    }
}
