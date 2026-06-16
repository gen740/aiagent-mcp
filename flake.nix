{
  description = "Docker-friendly combined filesystem and shell MCP server";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-darwin"
        "x86_64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312.withPackages (ps: [ ps.mcp ]);

        combined-mcp = pkgs.stdenvNoCC.mkDerivation {
          pname = "combined-mcp";
          version = "0.1.0";
          src = ./.;

          installPhase = ''
            runHook preInstall
            mkdir -p $out/lib/combined-mcp/src $out/bin
            cp -R src/combined_mcp $out/lib/combined-mcp/src/
            makeWrapper ${python}/bin/python $out/bin/combined-mcp \
              --add-flags "-m combined_mcp" \
              --set PYTHONPATH "$out/lib/combined-mcp/src" \
              --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.git ]}
            runHook postInstall
          '';

          nativeBuildInputs = [ pkgs.makeWrapper ];
        };
      in
      {
        default = combined-mcp;
        combined-mcp = combined-mcp;

        docker = pkgs.dockerTools.buildLayeredImage {
          name = "combined-mcp";
          tag = "latest";
          contents = [
            combined-mcp
            pkgs.bashInteractive
            pkgs.coreutils
            pkgs.findutils
            pkgs.git
            pkgs.gnugrep
            pkgs.gnused
          ];
          config = {
            Cmd = [ "${combined-mcp}/bin/combined-mcp" "/projects" ];
            WorkingDir = "/projects";
            Env = [
              "PATH=${pkgs.lib.makeBinPath [ combined-mcp pkgs.bashInteractive pkgs.coreutils pkgs.findutils pkgs.git pkgs.gnugrep pkgs.gnused ]}"
            ];
          };
        };
      });

      apps = forAllSystems (system:
        let
          pkg = self.packages.${system}.combined-mcp;
        in
        {
          default = {
            type = "app";
            program = "${pkg}/bin/combined-mcp";
          };
        });

      formatter = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        pkgs.writeShellApplication {
          name = "treefmt";
          runtimeInputs = [
            pkgs.ruff
            pkgs.treefmt
          ];
          text = ''
            exec treefmt "$@"
          '';
        });

      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python312.withPackages (ps: [ ps.mcp ]))
              pkgs.git
              pkgs.ruff
              pkgs.treefmt
              self.packages.${system}.combined-mcp
            ];
          };
        });
    };
}
