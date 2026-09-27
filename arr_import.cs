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
            bool ok = text != null && text.Contains("\"ok\":true");
            // NUARR SAID IT IS THERE; LOOK AGAIN BEFORE DISBELIEVING IT. The file
            // arrives at dst by a rename inside DrivePool's pool, and this process
            // asked "is it there?" a few milliseconds after nuarr's own check
            // passed - and 6 of 12 episodes in one pack got "no". DeferMove then
            // sent Sonarr to copy the source itself, which nuarr had already
            // moved away, and each of those sat in the queue as "file doesn't
            // exist". Give the pool a few seconds to show what nuarr has
            // verified is on the disk.
            bool there = false;
            if (ok)
            {
                for (int i = 0; i < 40 && !there; i++)
                {
                    there = File.Exists(dst);
                    if (!there) System.Threading.Thread.Sleep(100);
                }
            }
            if (ok && there) status = "MoveComplete";
            Log(arr + " " + status + (ok && !there ? " (nuarr said ok but dst not visible after 4s)" : "")
                + " | " + Path.GetFileName(dst) + " | " + (text == null ? "no answer" : text.Length > 160 ? text.Substring(0, 160) : text));
        }
        catch (Exception e) { status = "DeferMove"; Log("DeferMove on exception: " + e.GetType().Name + ": " + e.Message); }
        Console.WriteLine("[MoveStatus]" + status);
        return 0;
    }

    // One line per import, so the next time an entry sticks in a queue the
    // answer is in C:\nuarr\logs\arr_import.log rather than in a guess.
    static void Log(string line)
    {
        try
        {
            var p = @"C:\nuarr\logs\arr_import.log";
            Directory.CreateDirectory(Path.GetDirectoryName(p));
            var fi = new FileInfo(p);
            if (fi.Exists && fi.Length > 2 * 1024 * 1024) File.Copy(p, p + ".1", true);
            if (fi.Exists && fi.Length > 2 * 1024 * 1024) File.WriteAllText(p, "");
            File.AppendAllText(p, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + line + Environment.NewLine);
        }
        catch { }
    }
}
